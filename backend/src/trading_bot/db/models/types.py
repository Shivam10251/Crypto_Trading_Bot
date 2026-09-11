"""Shared column types.

Money, prices and quantities are ``NUMERIC``, never floats: a float cannot
represent 0.1 exactly, and rounding drift in balances or fills is unacceptable
in an audit trail. Floats appear only on derived statistics (Sharpe, win rate)
where precision loss is harmless.

Scales are sized for crypto: BTC trades near 100,000 with 8 decimals, while
some altcoins price below 0.00000001.
"""

from __future__ import annotations

from sqlalchemy import Numeric

# 28 digits total, 12 after the point - covers both extremes of crypto pricing.
PRICE = Numeric(28, 12)
QUANTITY = Numeric(28, 12)
# Fiat-denominated values: notional, fees, P&L, equity.
MONEY = Numeric(20, 8)
# Basis points (an edge of 3.5 bps); six decimals keeps sub-bps detail.
BPS = Numeric(14, 6)

SYMBOL_LENGTH = 32
