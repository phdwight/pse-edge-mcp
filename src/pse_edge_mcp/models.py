"""Pydantic models. Validation doubles as endpoint-change detection:
if PSE Edge changes shape, these raise loudly instead of returning garbage."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Meta(BaseModel):
    """Attached to every tool result: honesty about freshness."""

    as_of: datetime = Field(description="When this data was fetched from PSE Edge")
    valid_until: datetime | None = Field(
        description="Next market-close boundary; data frozen until then. "
        "Null when data_policy is 'immutable' (the object never changes upstream)."
    )
    from_cache: bool
    data_policy: Literal["EOD-frozen", "immutable"] = "EOD-frozen"
    stale: bool = Field(
        default=False,
        description="True when served past valid_until because the market is open "
        "(no upstream fetches during trading hours by design)",
    )


class CompanyHit(BaseModel):
    company_id: str
    name: str
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


class DisclosureHit(BaseModel):
    """One row of a disclosure search result (metadata only — no attachment fetch)."""

    edge_no: str = Field(description="Stable 32-hex disclosure key; pass to get_disclosure")
    template: str | None = Field(
        default=None, description="Disclosure type, e.g. 'Material Information/Transactions'"
    )
    company_name: str | None = None
    company_id: str | None = None
    announced_at: datetime | None = None
    pse_form_number: str | None = None
    circular_number: str | None = None


class DisclosureSearchResult(BaseModel):
    hits: list[DisclosureHit]
    page: int | None = None
    pages: int | None = Field(default=None, description="Total pages available upstream")
    total: int | None = Field(default=None, description="Total matching disclosures")
    has_more: bool = Field(default=False, description="True when further pages exist")
    source: Literal["announcements", "company_disclosures"] = Field(
        description="Which Edge endpoint served this page"
    )


class KeywordHit(BaseModel):
    """A full-text match inside a disclosure attachment."""

    edge_no: str
    subject: str | None = None
    company_name: str | None = None
    company_id: str | None = None
    announced_at: datetime | None = None
    circular_number: str | None = None
    attachment_file_id: str | None = None
    attachment_filename: str | None = None
    snippet: str | None = Field(default=None, description="Matched text with surrounding context")


class KeywordSearchResult(BaseModel):
    hits: list[KeywordHit]
    page: int | None = None
    pages: int | None = None
    total: int | None = None
    has_more: bool = False
    coverage_note: str = Field(
        default=(
            "PSE Edge's full-text index is partial and lags: at capture time it covered "
            "roughly 2023-2025 and contained nothing from 2026. For recent disclosures "
            "use search_disclosures instead."
        ),
        description="Honest limits of this endpoint, for relaying to the user",
    )


class DisclosureAttachment(BaseModel):
    file_id: str
    filename: str
    download_url: str = Field(description="Absolute URL; fetch it yourself if you need the file")


class DisclosureDocument(BaseModel):
    edge_no: str
    label: str
    is_current: bool = False


class DisclosureDetail(BaseModel):
    edge_no: str
    template: str | None = None
    company_name: str | None = None
    disclosure_date: date | None = None
    documents: list[DisclosureDocument] = Field(
        default_factory=list,
        description="Related documents in this disclosure's viewer (amendments, series)",
    )
    attachments: list[DisclosureAttachment] = Field(default_factory=list)
    body_html_url: str | None = Field(
        default=None,
        description="Absolute URL of the rendered disclosure body HTML. Not fetched or "
        "parsed by this server (v1 scope is metadata + links); fetch it if you need the text.",
    )
