"""Taker fee model. Source: docs.polymarket.com/trading/fees (checked 2026-11-14); verified on-chain
(implied rate 0.05 sports, 0.07 crypto from OrderFilled.fee). Makers pay zero.
Per-market truth is Gamma `feeSchedule.rate`; fall back to the category table in settings.
"""
from __future__ import annotations


def taker_fee(shares: float, price: float, rate: float) -> float:
    return round(shares * rate * price * (1.0 - price), 5)


def fee_per_share_bps_of_notional(price: float, rate: float) -> float:
    """Fee as bps of notional paid: rate*(1-p). E.g. sports @0.80 -> 100 bps."""
    return 1e4 * rate * (1.0 - price)
