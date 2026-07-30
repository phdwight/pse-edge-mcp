from __future__ import annotations

from datetime import date

import pytest

from pse_edge_mcp.errors import EndpointChangedError
from pse_edge_mcp.models import StockQuote
from pse_edge_mcp.parsers import parse_stock_data_page


def test_parse_stock_data_page(stock_data_html):
    parsed = parse_stock_data_page(stock_data_html)
    quote = StockQuote(**parsed)

    assert quote.company_id == "599"
    assert quote.security_id == "520"
    assert quote.symbol == "SM"
    assert quote.company_name == "SM Investments Corporation"
    assert quote.status == "Open"
    assert quote.last_traded_price == 847.00
    assert quote.open == 842.00
    assert quote.high == 851.00
    assert quote.low == 840.00
    assert quote.previous_close == 845.50
    assert quote.change == 1.50
    assert quote.change_percent == 0.18
    assert quote.volume == 605320
    assert quote.week52_high == 901.00
    assert quote.week52_low == 720.00
    assert quote.market_cap == 1_020_504_738_540.00
    assert quote.outstanding_shares == 1_204_582_867
    assert quote.isin == "PHY806761029"
    assert quote.listing_date == date(2005, 3, 22)
    assert quote.free_float_percent == 51.55
    assert quote.foreign_ownership_limit_percent == 100
    assert "Average Price" in quote.raw_fields  # unmapped labels still surface


def test_down_change_is_negative(stock_data_html):
    html = stock_data_html.replace("up 1.50 (0.18%)", "down 2.25 (0.27%)")
    parsed = parse_stock_data_page(html)
    assert parsed["change"] == -2.25
    assert parsed["change_percent"] == -0.27


def test_structure_change_raises_loudly():
    with pytest.raises(EndpointChangedError):
        parse_stock_data_page("<html><body><p>redesigned page</p></body></html>")
