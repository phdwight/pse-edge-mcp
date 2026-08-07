"""Pydantic models. Validation doubles as endpoint-change detection:
if PSE Edge changes shape, these raise loudly instead of returning garbage."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

DataPolicy = Literal["EOD-frozen", "daily-refresh", "immutable"]
"""How a cached value relates to upstream: price data is EOD-frozen (never fetched while
the market is open), everything else is daily-refresh (fetched at any hour, then reused
until the next close), and objects with an immutable natural key are fetched once ever."""


class Meta(BaseModel):
    """Attached to every tool result: honesty about freshness."""

    as_of: datetime = Field(description="When this data was fetched from PSE Edge")
    valid_until: datetime | None = Field(
        description="Next market-close boundary; the cached value is reused until then. "
        "Null when data_policy is 'immutable' (the object never changes upstream)."
    )
    from_cache: bool
    data_policy: DataPolicy = "EOD-frozen"
    stale: bool = Field(
        default=False,
        description="True when this is not a settled end-of-day value: served past "
        "valid_until (the market is open on price data, or PSE Edge was unreachable), "
        "or a price fetched mid-session because nothing was cached (see note)",
    )
    note: str | None = Field(
        default=None,
        description="Human-readable freshness caveat — e.g. a price fetched during the "
        "trading session that is not a realtime value. Null when there is nothing to flag.",
    )


class CompanyHit(BaseModel):
    company_id: str
    name: str
    symbol: str
    is_etf: bool = False


class SymbolValidation(BaseModel):
    """Answer to "is this a real PSE ticker" — a fact, not an error.

    `valid: false` with null fields rather than a raised SYMBOL_NOT_FOUND: an agent
    checking a symbol before using it is asking a question, and a failed lookup is a
    perfectly good answer to it.
    """

    valid: bool
    symbol: str = Field(description="The input, normalised to uppercase")
    company_name: str | None = None
    company_id: str | None = None


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


# --- Phase 3: company info & market -----------------------------------------


class CompanyProfile(BaseModel):
    company_id: str
    company_name: str
    sector: str | None = None
    subsector: str | None = None
    incorporation_date: date | None = None
    corporate_life: str | None = None
    number_of_directors: int | None = None
    fiscal_year: str | None = Field(default=None, description="Fiscal year end, as Month/Day")
    stockholders_meeting: str | None = None
    external_auditor: str | None = None
    transfer_agent: str | None = None
    business_address: str | None = None
    email: str | None = None
    telephone: str | None = None
    fax: str | None = None
    website: str | None = None
    raw_fields: dict[str, str] = Field(
        default_factory=dict, description="Every label->value pair on the page, unmapped included"
    )


class FinancialStatement(BaseModel):
    """One statement table (balance sheet or income statement) for one period.

    `columns` names the two value columns as Edge labels them — annual reports
    "Current Year"/"Previous Year", quarterly "Period Ended"/"Fiscal Year Ended(Audited)".
    `items` preserves Edge's own line-item labels and order rather than mapping them to a
    fixed schema, because the label set varies by company and industry.
    """

    statement: str = Field(description="e.g. 'Balance Sheet', 'Income Statement'")
    columns: list[str] = Field(default_factory=list)
    items: dict[str, list[float | None]] = Field(
        default_factory=dict, description="Line item -> one value per column, in column order"
    )


class FinancialPeriod(BaseModel):
    """Annual or quarterly section of the financial-reports page."""

    period_type: Literal["annual", "quarterly"]
    period_label: str | None = Field(
        default=None, description="Verbatim, e.g. 'For the fiscal year ended : Dec 31, 2025'"
    )
    period_ended: date | None = None
    currency_units: str | None = Field(
        default=None,
        description="Edge's own units label, e.g. 'Php (in thousands)'. Values are NOT "
        "rescaled — see the note on FinancialHighlights.",
    )
    statements: list[FinancialStatement] = Field(default_factory=list)


class FinancialHighlights(BaseModel):
    """Financial highlights as PSE Edge publishes them.

    **Values are reported exactly as Edge prints them and are never rescaled.** Each
    period carries its own `currency_units` label, and the labels differ between sections
    (observed 2026-07-30: annual said "Php (in thousands)" while quarterly said
    "Php (in Millions)" for the same company). Treat `currency_units` as the authority on
    scale, and be aware Edge's own label may not be reliable — do not present these as
    absolute peso amounts without checking it.
    """

    company_id: str
    company_name: str | None = None
    periods: list[FinancialPeriod] = Field(default_factory=list)
    note: str | None = Field(
        default=None, description="Any page-level notice, e.g. that no statements are filed yet"
    )


class DividendRecord(BaseModel):
    security_type: str | None = None
    dividend_type: str | None = Field(default=None, description="e.g. Cash, Stock, Property")
    dividend_rate: str | None = Field(
        default=None, description="Verbatim, e.g. 'Php17.00' or a percentage"
    )
    ex_dividend_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None
    circular_number: str | None = None
    edge_no: str | None = Field(
        default=None, description="Disclosure key for the notice; pass to get_disclosure"
    )


class RightsRecord(BaseModel):
    entitlement_ratio: str | None = None
    offer_price: str | None = None
    ex_rights_date: date | None = None
    offer_start: date | None = None
    offer_end: date | None = None
    circular_number: str | None = None
    edge_no: str | None = None


class DividendsAndRights(BaseModel):
    company_id: str
    dividends: list[DividendRecord] = Field(default_factory=list)
    rights: list[RightsRecord] = Field(default_factory=list)


class IndexQuote(BaseModel):
    name: str
    value: float | None = None
    change: float | None = Field(
        default=None,
        description="Signed. Edge prints this unsigned and encodes direction in a colour "
        "and a ▲/▼ glyph; the sign here is derived from that.",
    )
    change_percent: float | None = None
    direction: Literal["up", "down", "flat"] | None = None


class MarketIndices(BaseModel):
    indices: list[IndexQuote] = Field(default_factory=list)


class FeedItem(BaseModel):
    """One entry in a homepage feed."""

    title: str
    symbol: str | None = None
    company_id: str | None = None
    announced_at: datetime | None = None
    circular_number: str | None = None
    edge_no: str | None = None


class MarketSummary(BaseModel):
    """Whole-market snapshot from the PSE Edge homepage.

    `feeds` is keyed by Edge's own group labels — observed: `Company Announcements`,
    `Financial Reports`, `Other Reports`, `Listing Notices`, `Disclosure Notices`,
    `Today`, `This Week` — rather than folded into invented buckets, so a caller sees the
    site's own taxonomy and a new or renamed group appears instead of being dropped.

    Note: Edge publishes no gainers/losers/most-active data anywhere (verified Phase 0),
    so those are absent by necessity rather than oversight — the PSE main site, not Edge,
    would be the source.
    """

    indices: list[IndexQuote] = Field(default_factory=list)
    feeds: dict[str, list[FeedItem]] = Field(
        default_factory=dict, description="Edge's feed group label -> its entries"
    )
