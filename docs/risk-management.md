# Risk Management

Status: **implemented and reviewed (Phase 9).** The risk engine
(`trading_bot.risk`) sits in front of the execution adapter. Every signal -
real or shadow - is evaluated, and every decision is durably stored before
anything downstream may act on it. The fourteen safety defects found auditing
the first implementation are listed in
[development-phases.md](development-phases.md#what-the-audit-found).

## Position in the pipeline

```
Signal → Risk Engine → APPROVED → admit → bounded execution dispatcher
                     → REJECTED → durable risk event, no order
                     → PAUSED   → durable state, no order
```

**Three checkpoints, not one.** Anything that can change between deciding and
sending is checked at evaluation, again once the approval is durably stored,
and once more in `RiskEngine.admit` - which the coordinator calls in the
instant before it hands an order to an adapter. An approval that no longer
holds at any of those points is withdrawn: the reservation is released, a
durable risk event records the withdrawal against the approval it cancels,
and no order is created at all.

It is independent of the strategy on purpose: a buggy or over-confident
strategy must not be able to talk its way past a limit. Every check
re-derives its own answer - from the opportunity's own evidence, the
account's own ledger, the durable kill-switch state - rather than trusting
whatever the strategy already decided.

The engine lives inside `ExecutionDispatcher`'s workers, not the strategy
loop: `dispatcher.enqueue()` stays a non-blocking queue push, and the risk
evaluation's durable database write happens off that loop entirely, so a slow
or contended write never delays the next evaluation cycle.

**An order is never submitted unless its `APPROVED` decision was durably
stored first.** `RiskVerdict.is_approved` is `False` whenever the write to
`risk_events` could not be confirmed, regardless of what the decision itself
was - and the dispatcher checks exactly that property before ever calling the
coordinator. A `REJECTED` or `PAUSED` verdict never creates an order row at
all, not even a rejected one: nothing was ever going to be placed, so nothing
is recorded except the risk decision.

## What is enforced, without TOCTOU races

| Check | How | Enforced by |
| --- | --- | --- |
| Maximum order notional | per leg, against `max_order_notional_usd` | `PaperAccount.reserve` |
| Maximum aggregate position notional per market | per leg's market, against `max_position_notional_usd` | `PaperAccount.reserve` |
| Maximum total gross exposure | across both legs, against `max_total_exposure_usd` | `PaperAccount.reserve` |
| Sufficient cash, spot inventory, perpetual margin, borrow capacity | the same reservation | `PaperAccount.reserve` |
| Stale quote / book (per leg) | age **recomputed** from the stored timestamps against the current clock, vs. `max_stale_data_ms` | `risk.validation.check_temporal` |
| Stale or missing funding observation | age recomputed from `observed_at`, vs. `max_funding_age_ms` | `risk.validation.check_temporal` |
| Unsynchronised or incomplete market data | evidence must exist *and* agree with the opportunity - see below | `risk.validation.validate_evidence` |
| Maximum expected slippage | adverse slippage summed across the legs, vs. `max_slippage_bps` | `RiskEngine.evaluate` |
| Maximum decision/execution latency | time since the signal was generated vs. `max_latency_ms` | `risk.validation.check_temporal` |
| Signal expiry | `signal.expires_at` vs. now, at every checkpoint | `RiskEngine.evaluate` / `admit` |
| Execution queue overload | the dispatcher's bounded queue (`execution.queue_size`) rejecting on overflow | `ExecutionDispatcher` + `RiskEngine.record_queue_overload` |
| Kill switch / paused state | a durable flag, checked at all three checkpoints and *inside* the account's reservation lock | `KillSwitchState.guard` via `PaperAccount.reserve` |
| Post-fill order, position and gross exposure | actual settled notionals are checked again after fills | `PaperAccount.current_limit_breaches` / `RiskEngine.evaluate_post_trade` |

**Ages are measured, never read off the row.** An opportunity records how old
its inputs were when it was detected. By the time a worker evaluates it - and
again by the time an order is about to be sent - that number is a lower
bound, so every temporal limit is recomputed from the underlying timestamps
against the current clock. A signal that waited in a queue cannot present a
stale book as fresh.

**Evidence is validated for consistency, not just presence.** Each leg's
evidence must describe the same market, side, quantity, reference price and
executable price as the leg it claims to price. Both entry and modelled-unwind
walks must be complete, use the correct side, and have levels that reproduce
their quantity and VWAP; quotes and displayed sizes must be positive and
finite; quotes must be uncrossed; books must carry a real sequence; timestamps
must be timezone-aware and no decision timestamp may be in the future; and a
priced perpetual leg must carry a funding observation.

**Reused, not reimplemented.** `PaperAccount` already had to reserve
atomically against order, position, exposure, cash, margin and borrow limits
for Phase 8's concurrent execution workers - that lock-protected reservation
is exactly what a risk engine needs, so `RiskEngine.evaluate` calls into it
rather than keeping a second, competing ledger. The one thing `PaperAccount`
did not know about was the kill switch: `KillSwitchState.guard` is passed
into `reserve` and checked *inside* the same lock, before the idempotency
shortcuts and before any limit is evaluated. That is what closes
the kill-switch race - checking the switch a moment earlier, outside the
lock, would leave a window between the check and the reservation for the
switch to flip in.

Retrying an in-progress evaluation is idempotent two ways:
`PaperAccount.reserve` returns its existing reservation after rechecking the
kill guard, and `risk_events` carries a
`UNIQUE (mode, intent_id, event_type)` constraint, so a retried write
converges on the row already there instead of duplicating it.
An aborted pre-submission reservation is not marked completed: a later retry
must run every resource limit again. Only an execution attempt that reached
settlement enters the completed-intent cache.

## Every decision, durably

Every `RiskEvent` row carries:

- a stable **identity** (`intent_id` - the same execution-intent id shared
  with the order it approves, or the work item it rejects)
- a **timestamp** (`occurred_at`)
- a **reason code** (`event_type` - `ORDER_SIZE_EXCEEDED`,
  `POSITION_LIMIT_EXCEEDED`, `EXPOSURE_LIMIT_EXCEEDED`,
  `INSUFFICIENT_RESOURCES`, `STALE_DATA`, `INCOMPLETE_MARKET_DATA`,
  `SLIPPAGE_EXCEEDED`, `LATENCY_EXCEEDED`, `SIGNAL_EXPIRED`,
  `QUEUE_OVERLOAD`, `KILL_SWITCH`, `DAILY_LOSS_LIMIT`, `CONSECUTIVE_LOSSES`,
  `ABNORMAL_EXECUTION`, `FAIL_CLOSED`, or `PRE_TRADE_CHECK` for an approval)
- the **observed value** and the **configured limit** it was measured against
- **supporting evidence** (`context`, JSONB - decision latency, expected
  slippage, gross exposure, or a post-trade attempt's full set of findings)
- the **execution-intent / opportunity identity** (`intent_id`,
  `opportunity_uid`) - carried directly rather than by foreign key, the same
  reason `orders.opportunity_uid` exists: a signal/opportunity row may not be
  written yet when the decision is made
- whether it is a **shadow probe** (`is_shadow`)

`orders.risk_event_id` links an order back to the decision that allowed it -
the risk engine hands the id it just persisted to
`ExecutionCoordinator.execute`, which threads it onto every `OrderRequest`
and from there onto the stored row.

## Fail-closed

Database or risk-state uncertainty always refuses the trade, never guesses:

- If persisting an `APPROVED` decision cannot be confirmed, the reservation
  it just made is released and the signal is denied
  (`FAIL_CLOSED`/`REJECTED`) - an unconfirmed approval is not an approval.
- If the kill switch cannot be loaded at startup (the database is
  unreachable), it loads as **active** - trading stays halted until an
  operator can see why, rather than assuming clear.
- A re-arm is only honoured once its audit write is confirmed; if it cannot
  be, the switch stays engaged. A *trigger* is the opposite: it takes effect
  in-process immediately, because halting is the safe direction and must not
  wait on a database round trip - the audit write still happens, and a
  failure to record it is logged critically.

## Kill switch

A durable, audited halt on actionable execution - `trading_bot.risk.kill_switch.KillSwitchState`.
Current state is derived from the most recent `risk_events` row of type
`KILL_SWITCH`. All trigger/re-arm writers take one PostgreSQL transaction-level
advisory lock before inserting, so concurrent transitions have a serialized
order without trusting process clocks or incorrectly treating an
uncoordinated sequence allocation as commit order.

**Observed, not just written.** Current state is a cache of the durable row,
refreshed by `KillSwitchState.run` every `risk.kill_switch_poll_ms` (default
1 s). That interval is the guarantee: a kill written by the CLI, or by a
second service, stops this one within one poll. A kill triggered *in* this
process takes effect immediately and does not wait for its own write.

### What a kill guarantees, precisely

- **New actionable work is refused at the door.** `enqueue` consults the
  switch, so halted work is never queued; the refusal is audited.
- **Accepted but unsubmitted work is dropped.** Activation notifies
  listeners, and `ExecutionDispatcher.purge` drains queued actionable items,
  writes a `KILL_SWITCH`/`PAUSED` event for each, and releases their claims
  so the same episode can be re-offered after a re-arm. Shadow probes stay
  queued: they are isolated from the actionable halt.
- **Approved-but-unsent work is withdrawn.** `RiskEngine.admit` refuses in
  the instant before submission, releasing the reservation. No order row is
  written, because no order was created.
- **Submitted work is asked to cancel.** `ExecutionCoordinator.cancel_in_flight`
  calls the adapter's `cancel` for every order handed over and not yet
  answered for. Paper orders do not remain resting after `submit` returns, but
  they are in flight during simulated latency and can be cancelled in that
  interval. The registry tracks individual orders across concurrent workers.
- **Already-filled exposure is not unwound.** A kill stops new orders; it
  does not close positions. Nothing here invents an unwind - that is Phase
  10's decision, and pretending otherwise would be the dishonest kind of
  safety.
- **Every trigger and re-arm is audited**: who, why, and whether the write
  was durable, via `trading-bot-risk kill --who ... --reason ...` and
  `trading-bot-risk rearm --who ... --reason ...`.
- **No unauthenticated public endpoint.** Nothing in this codebase
  authenticates an HTTP caller yet, so re-arming is a CLI command requiring
  shell access to the host, not an API route. The dashboard (Phase 12) can
  wrap it once an authenticated surface exists.
- **A failed command exits nonzero.** If the row could not be written, the
  CLI says so and exits 1 in both directions - a kill nobody recorded is a
  kill no service will observe, and an operator must not be told otherwise.
- **"Current" does not trust a process clock.** Trigger/re-arm transactions
  are serialized before their insert, and the restore query orders those
  serialized rows by database id. A machine whose clock runs fast cannot make
  an older decision look current.
- **Auto-pause on naked exposure**: a non-shadow attempt that leaves one leg
  unhedged trips the same kill switch (`source: "auto_pause_unhedged"` in its
  context), controlled by `risk.pause_on_unhedged` (default `true`). Phase 8
  measured this happening in 3 of 21 limit-order attempts, so it is not a
  rare case worth leaving unhandled.

## Post-trade review

After execution, `RiskEngine.evaluate_post_trade` looks at what actually
happened - not what was expected - and persists a row only when something
needs review, never a routine confirmation (a clean, fully hedged, on-time
attempt is already fully described by its own order and fill rows):

| Finding | Response |
| --- | --- |
| **Naked exposure** (`unhedged_quantity > 0`) | pause (`pause_on_unhedged`) |
| **Realised slippage** past `max_slippage_bps` (adverse, summed across legs) | pause (`pause_on_abnormal_execution`) |
| **Execution latency** on either leg past `max_latency_ms` | pause (`pause_on_abnormal_execution`) |
| **Adapter failure or timeout** | pause (`pause_on_abnormal_execution`) |
| **Cross-leg fill-time skew** past its bound | **recorded only** |

The first four all mean the same thing in different words: execution is not
behaving the way the approval assumed, which is what the architecture's "fail
safe" principle exists for. Skew is the deliberate exception. Its exposure
consequence is already covered - skew that actually broke the hedge appears
as naked exposure and pauses under that rule, while skew that did not is a
timing measurement. Phase 8 measured skew on every attempt; halting on it
would stop trading for a condition with no effect on the book.

A partial or one-leg fill is therefore never silent: it is visible both as
the naked side of an `ExecutionAttempt` (Phase 8) and, when it is a real
attempt, as a durable, reviewable `risk_events` row. Shadow attempts are
excluded from post-trade review entirely - see below.

**Slippage means one thing, before and after.** Both the pre-trade estimate
and the post-trade measurement are the sum of *adverse* per-leg slippage, so
a leg that filled better than expected can never net off one that filled
worse, and the two numbers are comparable against the same limit.

## Daily-loss and consecutive-loss limits: honestly deferred

Phase 10 does not exist yet, so there is no trustworthy realised P&L to
measure a loss against. `RiskEngine` never evaluates these limits against a
fabricated zero, which would silently report "not losing" on every signal.
Instead, a `PnlSource` interface (`realized_pnl_today_usd`,
`consecutive_losses`) says whether real numbers exist, and configuration
(`risk.daily_loss_policy`, `risk.consecutive_loss_policy`) decides what
happens while they do not:

- `"deferred"` (the default): the limit is not enforced, **and every
  approval says so**. Each approved decision's context carries
  `deferred_controls` naming exactly which controls were not evaluated, and
  its reason reads "within every enforced limit; not evaluated: ..." rather
  than claiming every configured limit passed.
- `"fail_closed"`: every signal is rejected (`DAILY_LOSS_LIMIT` /
  `CONSECUTIVE_LOSSES`, with the reason naming the missing P&L source) until
  a real `PnlSource` is wired in. This is a way to say "do not trade without
  this control operating," not a way to make the limit operate.

When a real `PnlSource` does exist, a breach does more than refuse one
signal:

- **a daily-loss breach halts trading** for `risk.daily_loss_halt_minutes`
  (default 1440 - the day). The halt carries its own expiry, so it clears on
  its own terms, and a restart restores it with the window intact.
- **a consecutive-loss breach is a durable pause.** No timer clears it: "the
  strategy has stopped working" needs review and an explicit, audited
  re-arm.

`NullPnlSource` is the only implementation today, and it always returns
`None`. There is no way to make `max_daily_loss_usd` or
`max_consecutive_losses` genuinely enforce a limit before Phase 10 supplies
real realised P&L - claiming otherwise would be exactly the kind of
misleading risk claim this phase exists to avoid.

## Shadow isolation

A shadow probe is evaluated and its decision is recorded
(`risk_events.is_shadow = true`), because it is research data. It never:

- consults or is gated by the kill switch, or the daily-loss /
  consecutive-loss checks (both `if not is_shadow` branches in
  `RiskEngine.evaluate`)
- touches actionable exposure at all - probes reserve against a **separate
  `PaperAccount`**, so a probe can neither consume the capacity a real signal
  needs nor be rejected by a real signal that has consumed it. That account
  is never restored from durable positions (shadow positions are
  hypothetical, and seeding from them would carry yesterday's hypotheses into
  today's measurements) and is released rather than settled after every
  probe
- receives a post-trade review, or can trip the auto-pause policy
- is dropped when the actionable kill switch trips: a halt stops trading,
  and probes are research

A probe evaluated without an isolated ledger configured is refused
(`FAIL_CLOSED`) rather than quietly borrowing the trading one.

Shadow risk events and positions stay queryable separately by
`is_shadow`/`mode`, the same column already used to keep shadow orders and
positions out of every question about what the strategy actually earned.
Shadow decisions are also left unlinked from `signals`, because a probe's
signal is one the strategy declined to make.

## Configured limits

All in `config/base.yaml`, overridable per profile and validated at startup
(`RiskConfig` rejects inconsistent hierarchies, e.g. an order limit larger
than the position limit):

| Limit | Default | Guards against |
| --- | --- | --- |
| `max_order_notional_usd` | 1,000 | Fat-finger and runaway sizing |
| `max_position_notional_usd` | 5,000 | Concentration in one instrument |
| `max_total_exposure_usd` | 10,000 | Portfolio-wide leverage |
| `max_daily_loss_usd` | 200 | Losing day compounding - **deferred**, see above |
| `max_consecutive_losses` | 5 | A strategy that has stopped working - **deferred**, see above |
| `max_slippage_bps` | 15 | Executing into thin books (adverse, summed across legs) |
| `max_latency_ms` | 500 | Acting on information that has aged out |
| `max_stale_data_ms` | 2,000 | Trading on a frozen feed |
| `max_funding_age_ms` | 600,000 | Retaining a slow REST funding observation indefinitely |
| `kill_switch_poll_ms` | 1,000 | Bounds how long another process's kill takes to stop this one |
| `daily_loss_policy` | `deferred` | `deferred` \| `fail_closed`, see above |
| `consecutive_loss_policy` | `deferred` | `deferred` \| `fail_closed`, see above |
| `daily_loss_halt_minutes` | 1,440 | How long a daily-loss breach halts trading for |
| `pause_on_unhedged` | `true` | Whether naked exposure halts further entries |
| `pause_on_abnormal_execution` | `true` | Whether realised slippage/latency or an adapter failure halts further entries |

The execution queue's bound is `execution.queue_size`, and only that: the
risk engine reports the dispatcher's own queue size when it audits an
overload, so there is no second setting that could disagree with the queue
actually in use.

## Data-quality gates

Bad data is treated as a risk event, not an edge case - re-derived
independently of the strategy's own freshness checks, from the same evidence
the strategy recorded:

- a leg's quote or book older than `max_stale_data_ms`
- a funding observation older than `max_funding_age_ms` (separate because the
  funding REST feed intentionally refreshes every 60 seconds)
- an opportunity carrying no evidence at all (`INCOMPLETE_MARKET_DATA`) -
  which also covers a book the engine never got to vouch for as synced,
  since evidence is only ever built from a synced snapshot

## Safety properties already enforced

- Live execution is impossible outside an explicitly armed production process.
- Settings are frozen after resolution: limits cannot drift at runtime.
- Credential-shaped log fields are redacted centrally.
- The API starts even when the database is down, and reports `503` from
  `/health/ready` — monitoring stays reachable during an outage.
- An order is never submitted unless its `APPROVED` decision was durably
  stored first (this phase).
- The kill switch fails closed (active) if its state cannot be loaded, and a
  re-arm is only honoured once it is durably recorded (this phase).
