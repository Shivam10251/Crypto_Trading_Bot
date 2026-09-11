You are the lead software architect, senior Python engineer, quantitative developer, and systems engineer for this project.

We are building a completely NEW project from scratch.

There is NO existing codebase to preserve.
There is NO existing database to reuse.
There is NO existing dashboard to reuse.
There is NO existing trading engine.
There is NO existing architecture.

Treat the current project directory as a completely fresh project.

============================================================
PROJECT
============================================================

Build a modular crypto arbitrage research, paper-trading, backtesting,
analytics, and real-time monitoring platform.

The ultimate goal is to create a system capable of:

    Real-Time Market Data
            ↓
    Market Normalization
            ↓
    Arbitrage / Relative-Value Strategies
            ↓
    Opportunity Detection
            ↓
    Transaction Cost Analysis
            ↓
    Risk Management
            ↓
    Paper Execution
            ↓
    Trade / Position Management
            ↓
    P&L / Performance Analytics
            ↓
    Real-Time Dashboard

The initial system MUST NOT trade real money.

The system must first prove that the strategy can generate a realistic
edge under realistic assumptions.

Live trading should only become possible after extensive paper trading,
backtesting, validation, and explicit approval.

============================================================
VERY IMPORTANT — DEVELOPMENT METHOD
============================================================

DO NOT build the entire application in one step.

Build the system in PHASES.

Each phase must be:

- modular
- independently testable
- documented
- integrated carefully with previous phases
- completed before moving to the next phase

After completing each phase:

1. Explain what was built.
2. Show the directory structure.
3. Show files created/modified.
4. Explain important architectural decisions.
5. Run tests.
6. Show test results.
7. Fix any failures.
8. Explain known limitations.
9. Explain what the next phase will do.
10. STOP.

Do NOT automatically continue to the next phase.

Wait for my explicit instruction:

"Continue to Phase X"

before beginning the next major phase.

============================================================
CORE PRINCIPLE
============================================================

Do NOT build a fake trading dashboard around simulated numbers.

The dashboard must eventually display real data coming from:

Market Data
Strategy Engine
Risk Engine
Paper Execution
Database
Analytics Engine

During early development, use clearly labelled mock/test data only where
necessary.

Never present mock data as LIVE data.

============================================================
TECHNOLOGY PRINCIPLES
============================================================

Use Python as the primary backend language.

Prefer modern, maintainable Python.

Use:

- type hints
- async programming where appropriate
- dataclasses / Pydantic models where appropriate
- structured logging
- pytest
- environment variables
- configuration files
- clean dependency management

For the frontend, choose an appropriate modern web stack.

A reasonable default is:

Backend:
Python + FastAPI

Frontend:
React + TypeScript

Database:
PostgreSQL for the main application

Cache / high-speed state if needed:
Redis

However, do not blindly introduce technologies.

Use the simplest architecture that can support the requirements.

If you choose a different technology, explain why.

============================================================
HIGH-LEVEL ARCHITECTURE
============================================================

Design the application around independent modules:

                    ┌──────────────────┐
                    │ Binance / Market │
                    │ Data Providers   │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Market Data      │
                    │ Engine           │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Strategy Engine  │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Opportunity     │
                    │ Engine           │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Cost Model       │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Risk Engine      │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Execution Engine │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Database         │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Analytics        │
                    └────────┬─────────┘
                             ↓
                    ┌──────────────────┐
                    │ Dashboard        │
                    └──────────────────┘

IMPORTANT:

The Strategy Engine must NOT directly communicate with Binance.

The Strategy Engine generates signals.

The Risk Engine decides whether the signal is allowed.

The Execution Engine handles execution.

The Exchange Adapter handles communication with Binance.

This separation must be maintained throughout the project.

============================================================
PROPOSED DIRECTORY STRUCTURE
============================================================

Start with a clean structure similar to:

project-root/

├── README.md
├── .gitignore
├── .env.example
├── pyproject.toml
├── docker-compose.yml
│
├── docs/
│   ├── architecture.md
│   ├── development-phases.md
│   ├── strategy.md
│   ├── data-model.md
│   ├── risk-management.md
│   ├── execution.md
│   └── dashboard.md
│
├── config/
│   ├── settings.yaml
│   ├── markets.yaml
│   └── strategies.yaml
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   │
│   │   ├── core/
│   │   ├── api/
│   │   ├── market_data/
│   │   ├── strategies/
│   │   ├── opportunities/
│   │   ├── execution/
│   │   ├── risk/
│   │   ├── portfolio/
│   │   ├── analytics/
│   │   ├── storage/
│   │   └── exchanges/
│   │
│   └── tests/
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   ├── hooks/
│   │   ├── services/
│   │   ├── types/
│   │   └── layouts/
│   │
│   └── tests/
│
├── scripts/
│
├── data/
│   ├── raw/
│   ├── processed/
│   └── backtests/
│
└── docker/

You may modify this structure if you have a strong architectural reason.

Do not create unnecessary complexity.

============================================================
PHASE 0 — ARCHITECTURE & PROJECT FOUNDATION
============================================================

Create the project from scratch.

Do NOT connect to Binance yet.

Do NOT build the dashboard yet.

Do NOT implement trading yet.

Do NOT create strategy logic yet.

First:

1. Create the project structure.
2. Set up Python environment/dependency management.
3. Set up configuration management.
4. Set up environment variables.
5. Set up logging.
6. Set up testing.
7. Set up Git configuration.
8. Create initial documentation.
9. Create the initial database configuration.
10. Create the initial FastAPI application.
11. Create the initial frontend shell.
12. Make sure backend and frontend can start successfully.

Create:

.env.example

Never put API keys or secrets in source code.

At the end of Phase 0:

- backend starts
- frontend starts
- tests run
- configuration works
- project documentation exists

Then STOP.

============================================================
PHASE 1 — DATABASE & DATA MODEL
============================================================

Design the database from scratch.

Use PostgreSQL unless there is a strong reason not to.

Create migrations.

Design models/tables for at least:

markets
market_data
order_books
trades_market
opportunities
signals
orders
fills
positions
portfolio_snapshots
pnl_snapshots
risk_events
system_events

The database must allow us to trace:

Market Data
    ↓
Opportunity
    ↓
Signal
    ↓
Risk Decision
    ↓
Order
    ↓
Fill
    ↓
Position
    ↓
P&L

Use proper indexes.

Do not prematurely store every possible piece of raw market data forever.

Design retention policies where appropriate.

Create database tests.

Then STOP.

============================================================
PHASE 2 — EXCHANGE ABSTRACTION
============================================================

Create a generic exchange interface.

For example:

ExchangeAdapter

with capabilities such as:

get_markets()
get_ticker()
get_order_book()
subscribe_market_data()
place_order()
cancel_order()
get_order_status()
get_balances()

Then implement:

BinanceExchangeAdapter

Initially implement MARKET DATA functionality only.

Do not enable live order execution.

This abstraction should allow future support for:

Binance
Exchange B
Exchange C

without rewriting the strategy engine.

Then STOP.

============================================================
PHASE 3 — REAL-TIME MARKET DATA ENGINE
============================================================

Connect to Binance WebSocket market data.

Start with:

BTC/USDT

Then expand to configurable markets.

Eventually support 50+ markets.

Track:

- best bid
- best ask
- bid size
- ask size
- mid price
- spread
- volume
- timestamp
- exchange timestamp
- local timestamp
- latency

Implement:

- connection management
- reconnection
- heartbeat
- stale-data detection
- message validation
- normalization
- order-book synchronization

Use asynchronous architecture where appropriate.

Do NOT make the strategy depend directly on WebSocket implementation.

Create a clean internal MarketData model.

Create tests using recorded/mock messages.

At the end I should be able to run:

market-data service

and see real-time Binance data.

Then STOP.

============================================================
PHASE 4 — MARKET MONITORING
============================================================

Build the monitoring layer.

Support configurable markets.

For every market calculate:

mid price
bid/ask spread
spread %
volume
order-book imbalance
liquidity
data freshness
latency

Allow monitoring of 50+ markets.

Do not hard-code 50 symbols.

Configuration should control the markets.

Then STOP.

============================================================
PHASE 5 — STRATEGY FRAMEWORK
============================================================

Create a pluggable strategy architecture.

Define a base Strategy interface.

Example:

Strategy
├── initialize()
├── on_market_data()
├── detect_opportunity()
├── calculate_edge()
├── validate_signal()
└── generate_signal()

The strategy must not know about:

database implementation
Binance API
frontend
dashboard

It only consumes normalized market data and generates signals.

Create the first strategy:

BASIC ARBITRAGE / RELATIVE VALUE STRATEGY

The strategy should identify temporary price discrepancies between
related instruments/markets.

Do NOT immediately assume every discrepancy is profitable.

Then STOP.

============================================================
PHASE 6 — TRANSACTION COST MODEL
============================================================

Create a dedicated transaction-cost engine.

For every potential trade calculate:

gross edge
trading fees
estimated slippage
funding cost if applicable
borrow cost if applicable
latency buffer
other configured costs

Then:

NET EDGE =
GROSS EDGE
- FEES
- SLIPPAGE
- FUNDING
- OTHER COSTS
- SAFETY BUFFER

The strategy should be able to ask:

"What is the expected net profit after costs?"

Do not allow the strategy to use gross spread alone.

Then STOP.

============================================================
PHASE 7 — OPPORTUNITY ENGINE
============================================================

Build the opportunity pipeline.

For every potential opportunity create:

Opportunity ID
Timestamp
Strategy
Market
Direction
Entry price
Exit price
Quantity
Gross edge
Estimated costs
Net edge
Liquidity
Latency
Opportunity duration
Status

Statuses could include:

DETECTED
VALIDATED
REJECTED
PAPER_TRADE
EXPIRED
EXECUTED
FAILED

Store all opportunities.

Do NOT store only profitable ones.

We need complete research data.

Then STOP.

============================================================
PHASE 8 — PAPER EXECUTION ENGINE
============================================================

Create a realistic paper-trading system.

DO NOT simply assume:

signal = successful trade.

Simulate:

- bid/ask
- slippage
- latency
- partial fills
- liquidity
- rejected orders
- cancelled orders
- failed execution
- order timeout

Support:

BUY
SELL
OPEN
CLOSE
CANCEL
PARTIAL_FILL

Create:

PaperExecutionAdapter

The architecture should later allow:

LiveExecutionAdapter

without changing the strategy.

Then STOP.

============================================================
PHASE 9 — RISK ENGINE
============================================================

Create an independent risk engine.

Implement configurable limits:

maximum order size
maximum position
maximum exposure
maximum daily loss
maximum strategy loss
maximum consecutive losses
maximum slippage
maximum latency
maximum stale-data age

Create:

RiskDecision

Possible outcomes:

APPROVED
REJECTED
PAUSED

Every risk rejection must be logged.

Implement a kill switch.

Examples:

stale market data
    → reject trade

exposure limit reached
    → reject trade

daily loss limit reached
    → disable strategy

abnormal execution
    → pause strategy

Then STOP.

============================================================
PHASE 10 — PORTFOLIO & P&L
============================================================

Create portfolio management.

Track:

cash
positions
entry prices
exit prices
realized P&L
unrealized P&L
fees
slippage
equity

Calculate:

total return
win rate
profit factor
expectancy
maximum drawdown
Sharpe ratio
Sortino ratio
trade count
average trade
average win
average loss

Clearly distinguish:

THEORETICAL P&L
PAPER P&L
LIVE P&L

Do not mix them.

Then STOP.

============================================================
PHASE 11 — BACKTEST / REPLAY ENGINE
============================================================

Build a historical market-data replay system.

Architecture:

Historical Data
      ↓
Market Data Engine
      ↓
Strategy
      ↓
Cost Model
      ↓
Risk Engine
      ↓
Paper Execution
      ↓
Portfolio
      ↓
Analytics

IMPORTANT:

The strategy implementation must be reusable between:

BACKTEST

PAPER LIVE

LIVE

Do not create three separate versions of the strategy.

Then STOP.

============================================================
PHASE 12 — REAL-TIME DASHBOARD
============================================================

Now build the dashboard.

Use the provided screenshot as visual inspiration.

The dashboard should have a:

DARK
FUTURISTIC
QUANTITATIVE
TRADING TERMINAL

appearance.

Visual characteristics:

- black/dark background
- glass-like panels
- subtle neon accents
- purple/magenta highlights
- green positive values
- red negative values
- compact information density
- professional typography
- glowing status indicators
- real-time updates
- smooth animations
- responsive layout

Do NOT copy any branding.

Use the screenshot only as visual inspiration.

============================================================
DASHBOARD STRUCTURE
============================================================

TOP NAVIGATION

Display:

ARBITRAGE TERMINAL

Environment:

PAPER

Status:

● LIVE

Current time

Exchange:

BINANCE

Markets:

52

Data latency:

18 ms

------------------------------------------------------------

LEFT PANEL — BOT STATUS

Show:

BOT STATUS
Market Data
Strategy Engine
Risk Engine
Paper Execution
Database

Each should have:

HEALTHY
DEGRADED
OFFLINE

Also show:

Markets monitored
Opportunities detected
Signals generated
Paper trades
Current exposure

Controls:

START
PAUSE
STOP
KILL SWITCH

The controls must work through the backend.

------------------------------------------------------------

CENTER — PRIMARY MARKET

Large BTC/USDT display.

Show:

BTC price
24h change
bid
ask
spread
volume

Live chart.

Order-book visualization.

Market activity.

------------------------------------------------------------

RIGHT PANEL — PERFORMANCE

Show:

Today's P&L
Total P&L
Equity
Win Rate
Trades
Profit Factor
Sharpe
Sortino
Max Drawdown

Everything must come from backend data.

------------------------------------------------------------

LIVE OPPORTUNITIES

Create a real-time table:

Market
Strategy
Buy
Sell
Gross Edge
Fees
Slippage
Net Edge
Liquidity
Latency
Status

Allow:

sort
filter
search

Possible statuses:

🟢 TRADEABLE
🟡 WATCH
🔴 REJECTED
⚪ EXPIRED

------------------------------------------------------------

MARKET HEATMAP

Display monitored markets.

Show:

symbol
price movement
spread
opportunity state

Allow sorting/filtering.

------------------------------------------------------------

EQUITY CURVE

Show:

paper equity
drawdown

Time filters:

1D
7D
30D
ALL

------------------------------------------------------------

EXECUTION LOG

Live event stream.

Show:

timestamp
market
event
side
price
quantity
status
latency
P&L

Examples:

BUY
SELL
FILLED
PARTIAL
CANCELLED
REJECTED

------------------------------------------------------------

SYSTEM HEALTH

Show:

Market Data
Exchange
Database
Strategy
Risk
Execution
API

Use clear status indicators.

============================================================
PHASE 13 — DASHBOARD REAL-TIME BACKEND
============================================================

Create APIs and WebSocket streams.

Example endpoints:

/api/markets
/api/opportunities
/api/orders
/api/fills
/api/positions
/api/pnl
/api/performance
/api/risk
/api/system-status

Use WebSockets for real-time updates where appropriate.

Do not make the frontend repeatedly hammer the database.

============================================================
PHASE 14 — RESEARCH & STRATEGY ANALYTICS
============================================================

Build tools to analyze every opportunity.

Questions we should be able to answer:

How many opportunities occurred?

How many survived fees?

How many survived slippage?

How long did opportunities remain open?

What percentage were actually executable?

What was the average net edge?

What was the average execution latency?

How much theoretical profit existed?

How much paper profit was actually captured?

Where did we lose money?

Which markets work best?

Which times work best?

Which strategy parameters work best?

============================================================
PHASE 15 — STRATEGY EXPANSION
============================================================

Only after the basic strategy works.

Add strategies as independent modules:

1. Cross-market arbitrage
2. Triangular arbitrage
3. Spot vs perpetual arbitrage
4. Futures basis arbitrage
5. Statistical arbitrage
6. Order-book microstructure strategy

Each strategy must implement the same Strategy interface.

Do not merge all strategies into one file.

============================================================
PHASE 16 — PERFORMANCE OPTIMIZATION
============================================================

Only after measuring actual bottlenecks.

Investigate:

- asyncio
- efficient order-book structures
- memory usage
- CPU usage
- serialization
- database writes
- batching
- caching
- WebSocket performance
- latency

DO NOT prematurely optimize.

Measure first.

============================================================
PHASE 17 — LIVE TRADING ARCHITECTURE
============================================================

Create the infrastructure required for live trading but DO NOT activate it.

Architecture:

Strategy
   ↓
Risk Engine
   ↓
Execution Interface
      ↙        ↘
Paper          Live
Execution      Execution
               ↓
          Binance API

Live execution must be disabled by default.

The system must require explicit configuration to enable it.

API keys must never have withdrawal permissions.

============================================================
RISK & SAFETY REQUIREMENTS
============================================================

The system must have:

- kill switch
- max position
- max order size
- max exposure
- max daily loss
- stale data protection
- execution timeout
- API failure handling
- WebSocket reconnection
- duplicate-order protection
- order reconciliation
- position reconciliation
- emergency shutdown

If anything abnormal occurs:

FAIL SAFE.

Do not continue trading blindly.

============================================================
SECURITY
============================================================

Never hard-code:

API keys
API secrets
passwords
tokens

Use .env.

Never commit .env.

Create:

.env.example

Use separate configuration for:

development
paper
production

Live trading should be disabled in development.

============================================================
TESTING REQUIREMENTS
============================================================

Every major module requires tests.

Include:

unit tests
integration tests
strategy tests
execution tests
risk tests
database tests
API tests
frontend tests where appropriate

Important edge cases:

- zero liquidity
- stale data
- negative/invalid prices
- huge spreads
- partial fills
- failed orders
- duplicate messages
- WebSocket disconnect
- API timeout
- database failure
- extreme volatility
- simultaneous opportunities

Never say:

"Should work."

Actually test it.

============================================================
DOCUMENTATION
============================================================

Maintain:

README.md

and:

docs/

architecture.md
strategy.md
risk-management.md
execution.md
data-model.md
dashboard.md
development-phases.md

Documentation must be updated as the architecture evolves.

============================================================
GIT WORKFLOW
============================================================

Use Git from the beginning.

Create logical commits after completed phases.

Commit messages should be meaningful.

Examples:

feat: initialize trading platform
feat: add database models
feat: add Binance market data
feat: add arbitrage strategy
feat: add paper execution
feat: add risk engine
feat: add trading dashboard

Never commit:

.env
API keys
secrets
large generated datasets

============================================================
IMPORTANT DEVELOPMENT BEHAVIOR
============================================================

You are NOT allowed to:

- build everything at once
- skip tests
- create fake profitability
- fabricate trading results
- enable live trading automatically
- hard-code API keys
- hard-code fake dashboard values
- mix strategy and execution logic
- mix frontend and trading logic
- create giant files
- duplicate strategy implementations unnecessarily
- claim success without verification

You SHOULD:

- think architecturally
- keep modules independent
- write tests
- explain decisions
- keep configuration external
- make components replaceable
- use real market data when appropriate
- measure performance
- preserve reproducibility
- log important events
- fail safely

============================================================
MOST IMPORTANT DESIGN PRINCIPLE
============================================================

We are NOT trying to create a flashy dashboard first.

We are building a real quantitative research platform.

The priority is:

1. Correctness
2. Data quality
3. Strategy validity
4. Realistic execution simulation
5. Risk management
6. Reproducibility
7. Performance
8. Dashboard aesthetics

The dashboard is important, but it must visualize a real underlying system.

============================================================
FIRST INSTRUCTION
============================================================

START WITH PHASE 0 ONLY.

Create the project from scratch.

Do NOT connect to Binance yet.

Do NOT implement trading.

Do NOT implement arbitrage logic.

Do NOT build the full dashboard.

First create the foundation and architecture.

When Phase 0 is complete:

- show me the complete project tree
- explain every major component
- show how backend/frontend/database will communicate
- run the tests
- show the test results
- explain how I can start the project locally

Then STOP.

Wait for me to explicitly say:

"Continue to Phase 1"

before proceeding.