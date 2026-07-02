"""Load and serve the sector universe parsed from tickers_by_sector.txt.

File format (blocks separated by blank lines):

    XLK — Technology
    NVDA, AAPL, MSFT, ...

The first line of each block is the sector header (ETF symbol, a dash, then the
sector name); the following line(s) are the comma-separated constituents.
Parsed once at import and cached in memory. Provides the sector ETF list, each
sector's constituent tickers, and a stock -> sector-ETF reverse map used to
compute RS3M-vs-Sector for any candidate.
"""
from __future__ import annotations

import re
from functools import lru_cache

import config

# A header line is "<ETF> <dash> <name>"; dash may be em/en/hyphen.
_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.]{0,6})\s*[—–-]\s*(.+)$")


class Sector:
    def __init__(self, etf: str, name: str, group: str, tickers: list[str]):
        self.etf = etf
        self.name = name
        self.group = group
        self.tickers = tickers

    def as_dict(self) -> dict:
        return {"etf": self.etf, "name": self.name, "group": self.group, "tickers": self.tickers}


def _flush(sectors: dict, header: tuple[str, str] | None, ticker_lines: list[str]) -> None:
    if not header:
        return
    etf, name = header
    csv = ", ".join(ticker_lines)
    tickers = [t.strip().upper() for t in csv.split(",") if t.strip()]
    if tickers:
        group = config.SECTOR_GROUPS.get(etf, "")
        sectors[etf] = Sector(etf, name, group, tickers)


@lru_cache(maxsize=1)
def _load() -> dict[str, Sector]:
    sectors: dict[str, Sector] = {}
    header: tuple[str, str] | None = None
    ticker_lines: list[str] = []
    with open(config.TICKERS_BY_SECTOR_PATH, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                _flush(sectors, header, ticker_lines)
                header, ticker_lines = None, []
                continue
            m = _HEADER_RE.match(line)
            # A line is a header only if it has no comma (ticker lines are CSV).
            if m and "," not in line:
                _flush(sectors, header, ticker_lines)
                header = (m.group(1).upper(), m.group(2).strip())
                ticker_lines = []
            else:
                ticker_lines.append(line)
    _flush(sectors, header, ticker_lines)
    if not sectors:
        raise RuntimeError(f"no sectors parsed from {config.TICKERS_BY_SECTOR_PATH}")
    return sectors


def sectors() -> dict[str, Sector]:
    return _load()


def sector_etfs() -> list[str]:
    return list(_load().keys())


def constituents(etf: str) -> list[str]:
    s = _load().get(etf.upper())
    return list(s.tickers) if s else []


def all_tickers() -> list[str]:
    """Every constituent across every sector PLUS the sector ETFs themselves,
    de-duplicated, order-stable. The ETFs are liquid, weekly-optionable
    tickers in their own right, so they're valid CFM candidates alongside
    their constituents — every scan (Scorecard, Ready-to-Enter, calibration)
    sweeps this list, so including them here is what makes them selectable
    everywhere without separate wiring per caller."""
    seen: dict[str, None] = {}
    for etf, s in _load().items():
        seen.setdefault(etf, None)
        for t in s.tickers:
            seen.setdefault(t, None)
    return list(seen.keys())


@lru_cache(maxsize=1)
def stock_to_sector() -> dict[str, str]:
    """ticker -> sector ETF. ETFs map to themselves so lookups are total."""
    out: dict[str, str] = {}
    for etf, s in _load().items():
        out[etf] = etf
        for t in s.tickers:
            out.setdefault(t, etf)
    return out


def sector_for(ticker: str) -> str | None:
    return stock_to_sector().get(ticker.upper())
