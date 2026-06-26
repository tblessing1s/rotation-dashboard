"""Load and serve the sector universe parsed from data/tickers_by_sector.txt.

Parsed once at import and cached in memory. Provides the sector ETF list, each
sector's constituent tickers, and a stock -> sector-ETF reverse map used to
compute RS3M-vs-Sector for any candidate.
"""
from __future__ import annotations

from functools import lru_cache

import config


class Sector:
    def __init__(self, etf: str, name: str, group: str, tickers: list[str]):
        self.etf = etf
        self.name = name
        self.group = group
        self.tickers = tickers

    def as_dict(self) -> dict:
        return {"etf": self.etf, "name": self.name, "group": self.group, "tickers": self.tickers}


@lru_cache(maxsize=1)
def _load() -> dict[str, Sector]:
    sectors: dict[str, Sector] = {}
    with open(config.TICKERS_BY_SECTOR_PATH, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 4:
                continue
            etf, name, group, ticker_csv = parts
            tickers = [t.strip().upper() for t in ticker_csv.split(",") if t.strip()]
            sectors[etf.upper()] = Sector(etf.upper(), name, group, tickers)
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
    """Every constituent across every sector, de-duplicated, order-stable."""
    seen: dict[str, None] = {}
    for s in _load().values():
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
