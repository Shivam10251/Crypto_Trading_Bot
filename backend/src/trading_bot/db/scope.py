"""Which results a query may see: one mode, and at most one backtest run.

Every store that reads or writes a result table filters on both halves.
``mode`` keeps THEORETICAL, PAPER, LIVE and BACKTEST apart; ``backtest_run_id``
keeps two backtests apart, because they share a mode and - being deterministic
replays - the same client order ids, intent ids and snapshot instants.

Outside a backtest the run is ``None`` and the filter is ``IS NULL``, never
"any run": a paper query that forgot the run would otherwise sum every
backtest into the paper account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute

from trading_bot.db.models.enums import ExecutionMode


@dataclass(frozen=True, slots=True)
class RunScope:
    mode: ExecutionMode
    backtest_run_id: int | None = None

    def __post_init__(self) -> None:
        if (self.mode is ExecutionMode.BACKTEST) != (self.backtest_run_id is not None):
            raise ValueError(
                "a BACKTEST scope needs a run, and only a BACKTEST scope may have one "
                f"(mode={self.mode.value}, backtest_run_id={self.backtest_run_id})"
            )

    @classmethod
    def backtest(cls, run_id: int) -> RunScope:
        return cls(ExecutionMode.BACKTEST, run_id)

    @property
    def is_backtest(self) -> bool:
        return self.backtest_run_id is not None

    def run_filter(self, column: InstrumentedAttribute[Any]) -> ColumnElement[bool]:
        """``column IS NULL`` outside a run, ``column = run`` inside one."""
        if self.backtest_run_id is None:
            return column.is_(None)
        return column == self.backtest_run_id

    def filters(
        self, mode_column: InstrumentedAttribute[Any], run_column: InstrumentedAttribute[Any]
    ) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        return (mode_column == self.mode, self.run_filter(run_column))

    def values(self) -> dict[str, Any]:
        """The two columns a row written in this scope must carry."""
        return {"mode": self.mode, "backtest_run_id": self.backtest_run_id}
