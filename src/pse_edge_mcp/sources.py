"""Narrow interfaces onto PSE Edge, one per data domain.

`PseEdgeClient` structurally satisfies all of these, so nothing needs to declare an
inheritance relationship. Splitting the client's surface this way is deliberate:

- **Interface segregation:** the disclosure repository cannot reach a quote endpoint,
  because its dependency does not expose one. A fat "client" interface would let any
  layer call anything, and every fake in a test would have to stub all of it.
- **Dependency inversion:** the domain layer depends on these declarations rather than
  on the concrete HTTP client, so repositories are testable with a few-line fake and
  no HTTP mocking at all.

Return types are deliberately raw (`str` of HTML, `dict` of JSON) — this is the
transport boundary. Turning bytes into meaning is `parsers.py`' job, and turning that
into a validated model is the repository's.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class CompanySource(Protocol):
    async def search_companies(self, query: str) -> list[dict[str, Any]]: ...


class QuoteSource(Protocol):
    async def fetch_stock_data_page(self, company_id: str) -> str: ...

    async def fetch_price_history(
        self, company_id: str, security_id: str, start: date, end: date
    ) -> dict[str, Any]: ...


class DisclosureSource(Protocol):
    async def search_announcements(
        self,
        *,
        from_date: date,
        to_date: date,
        company_id: str = "",
        template: str = "",
        keyword: str = "",
        page: int = 1,
    ) -> str: ...

    async def search_company_disclosures(
        self, company_id: str, *, template: str = "", page: int = 1
    ) -> str: ...

    async def search_disclosure_fulltext(
        self,
        keyword: str,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        company_id: str = "",
        subject_title: str = "",
        sector: str = "",
        subsector: str = "",
        page: int = 1,
    ) -> str: ...

    async def fetch_disclosure_viewer(self, edge_no: str) -> str: ...
