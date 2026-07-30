"""Pydantic models. Validation doubles as endpoint-change detection:
if PSE Edge changes shape, these raise loudly instead of returning garbage."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Meta(BaseModel):
    """Attached to every tool result: honesty about freshness."""

    as_of: datetime = Field(description="When this data was fetched from PSE Edge")
    valid_until: datetime = Field(description="Next market-close boundary; data frozen until then")
    from_cache: bool
    data_policy: Literal["EOD-frozen"] = "EOD-frozen"
    stale: bool = Field(
        default=False,
        description="True when served past valid_until because the market is open "
        "(no upstream fetches during trading hours by design)",
    )


class CompanyHit(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    company_id: str = Field(alias="cmpyId")
    name: str = Field(alias="cmpyNm")
    symbol: str
    is_etf: bool = False


class StockQuote(BaseModel):
    symbol: str
    company_name: str
    company_id: str
    security_id: str
    status: str | None = None
    last_traded_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    value: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    market_cap: float | None = None
    outstanding_shares: int | None = None
    par_value: float | None = None
    isin: str | None = None
    listing_date: date | None = None
    free_float_percent: float | None = None
    foreign_ownership_limit_percent: float | None = None
    raw_fields: dict[str, str] = Field(
        default_factory=dict,
        description="All label->value pairs parsed from the page, for anything not mapped above",
    )


class OhlcBar(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    value: float


class PriceHistory(BaseModel):
    symbol: str
    company_id: str
    security_id: str
    start_date: date
    end_date: date
    bars: list[OhlcBar]
