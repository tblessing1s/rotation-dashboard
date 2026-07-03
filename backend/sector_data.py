"""Load and serve the sector universe.

The universe lives as an editable JSON store on the volume
(``config.UNIVERSE_PATH``), seeded once from the read-only repo file
(``tickers_by_sector.txt``) on first load. That makes it manageable at runtime —
add / remove / fix a ticker via the API without editing the repo and
redeploying — while surviving deploys (it's on the /data volume). If the store
is ever missing it self-heals by re-seeding from the repo file, so the baked-in
list is always the safety net.

Seed file format (blocks separated by blank lines):

    XLK — Technology
    NVDA, AAPL, MSFT, ...

Provides the sector ETF list, each sector's constituents, and a stock ->
sector-ETF reverse map used to compute RS3M-vs-Sector for any candidate.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import config

# A header line is "<ETF> <dash> <name>"; dash may be em/en/hyphen.
_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.]{0,6})\s*[—–-]\s*(.+)$")
UNIVERSE_SCHEMA = 1


class Sector:
    def __init__(self, etf: str, name: str, group: str, tickers: list[str]):
        self.etf = etf
        self.name = name
        self.group = group
        self.tickers = tickers

    def as_dict(self) -> dict:
        return {"etf": self.etf, "name": self.name, "group": self.group, "tickers": self.tickers}


# ---------------------------------------------------------------------------
# Seed (repo file) -> ordered [{etf, name, tickers}]
# ---------------------------------------------------------------------------
def _flush(out: list, header: tuple[str, str] | None, ticker_lines: list[str]) -> None:
    if not header:
        return
    etf, name = header
    tickers = [t.strip().upper() for t in ", ".join(ticker_lines).split(",") if t.strip()]
    if tickers:
        out.append({"etf": etf, "name": name, "tickers": tickers})


def _seed_from_file() -> list[dict]:
    out: list[dict] = []
    header: tuple[str, str] | None = None
    ticker_lines: list[str] = []
    with open(config.TICKERS_BY_SECTOR_PATH, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                _flush(out, header, ticker_lines)
                header, ticker_lines = None, []
                continue
            m = _HEADER_RE.match(line)
            # A line is a header only if it has no comma (ticker lines are CSV).
            if m and "," not in line:
                _flush(out, header, ticker_lines)
                header = (m.group(1).upper(), m.group(2).strip())
                ticker_lines = []
            else:
                ticker_lines.append(line)
    _flush(out, header, ticker_lines)
    if not out:
        raise RuntimeError(f"no sectors parsed from {config.TICKERS_BY_SECTOR_PATH}")
    return out


# ---------------------------------------------------------------------------
# Volume store (JSON) — read / write / seed
# ---------------------------------------------------------------------------
def _read_store() -> dict | None:
    """The raw store: {"sectors": [...], "removed": [...]} or None if absent."""
    try:
        with open(config.UNIVERSE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        sectors = data.get("sectors")
        if not (isinstance(sectors, list) and sectors):
            return None
        removed = [str(t).strip().upper() for t in (data.get("removed") or [])]
        return {"sectors": sectors, "removed": removed}
    except (OSError, ValueError):
        return None


def _write_store(sectors: list[dict], removed) -> None:
    import logging_handler as log  # reuse the atomic-write machinery (fsync + rename)
    payload = json.dumps({"schema": UNIVERSE_SCHEMA, "sectors": sectors,
                          "removed": sorted(set(removed))}, indent=2)
    log._atomic_write(config.UNIVERSE_PATH, payload)


def _current_removed() -> set:
    store = _read_store()
    return set(store["removed"]) if store else set()


def _merge_seed(sectors: list[dict], removed: set) -> bool:
    """Additively fold new entries from the repo seed file into an existing store:
    add any seed SECTOR the store lacks, and any seed TICKER not already present
    anywhere AND not tombstoned (so a name the operator removed stays gone, but a
    name newly added to the seed — e.g. the ETFs, or a future S&P addition —
    shows up automatically). Never removes or moves anything. Returns True if the
    store changed."""
    present = set()
    for s in sectors:
        present.add(str(s.get("etf", "")).upper())
        present.update(str(t).upper() for t in s.get("tickers", []))
    by_etf = {str(s.get("etf", "")).upper(): s for s in sectors}
    changed = False
    for seed_sec in _seed_from_file():
        etf = seed_sec["etf"].upper()
        store_sec = by_etf.get(etf)
        if store_sec is None and etf not in removed:
            store_sec = {"etf": etf, "name": seed_sec["name"], "tickers": []}
            sectors.append(store_sec)
            by_etf[etf] = store_sec
            present.add(etf)
            changed = True
        if store_sec is None:
            continue
        for t in seed_sec["tickers"]:
            t = t.upper()
            if t not in present and t not in removed:
                store_sec["tickers"].append(t)
                present.add(t)
                changed = True
    return changed


def _clear_caches() -> None:
    _load.cache_clear()
    stock_to_sector.cache_clear()


@lru_cache(maxsize=1)
def _load() -> dict[str, Sector]:
    store = _read_store()
    if store is None:  # first run (or store lost) — seed from the repo file
        raw, removed, dirty = _seed_from_file(), set(), True
    else:
        raw, removed = store["sectors"], set(store["removed"])
        dirty = _merge_seed(raw, removed)  # pull in anything new from the seed
    if dirty:
        try:
            _write_store(raw, removed)
        except OSError:  # read-only fs: still serve from memory this run
            pass
    sectors: dict[str, Sector] = {}
    for s in raw:
        etf = str(s.get("etf", "")).upper()
        if not etf:
            continue
        tickers = [t.strip().upper() for t in s.get("tickers", []) if str(t).strip()]
        sectors[etf] = Sector(etf, s.get("name", ""), config.SECTOR_GROUPS.get(etf, ""), tickers)
    if not sectors:
        raise RuntimeError("no sectors in the universe store")
    return sectors


# ---------------------------------------------------------------------------
# Mutations (runtime universe management) — persist + invalidate caches
# ---------------------------------------------------------------------------
def _sectors_as_list() -> list[dict]:
    return [{"etf": s.etf, "name": s.name, "tickers": list(s.tickers)} for s in _load().values()]


def add_ticker(ticker: str, sector: str) -> dict:
    """Add a constituent to a sector. Rejects unknown sectors and duplicates."""
    ticker = (ticker or "").strip().upper()
    sector = (sector or "").strip().upper()
    if not ticker:
        raise ValueError("ticker is required")
    sectors = _load()
    if sector not in sectors:
        raise ValueError(f"unknown sector '{sector}' (expected one of {sorted(sectors)})")
    existing = sector_for(ticker)
    if existing:
        raise ValueError(f"{ticker} is already in the universe ({existing})")
    lst = _sectors_as_list()
    for s in lst:
        if s["etf"] == sector:
            s["tickers"].append(ticker)
    removed = _current_removed()
    removed.discard(ticker)   # re-adding clears any tombstone
    _write_store(lst, removed)
    _clear_caches()
    return {"added": ticker, "sector": sector}


def remove_ticker(ticker: str) -> dict:
    """Remove a constituent. Sector ETFs (the headers) can't be removed. The name
    is tombstoned so a seed-merge won't re-add it on the next load."""
    ticker = (ticker or "").strip().upper()
    sectors = _load()
    if ticker in sectors:
        raise ValueError(f"{ticker} is a sector ETF — can't remove a sector header")
    lst = _sectors_as_list()
    removed_from = None
    for s in lst:
        if ticker in s["tickers"]:
            s["tickers"].remove(ticker)
            removed_from = s["etf"]
    if removed_from is None:
        raise ValueError(f"{ticker} is not in the universe")
    removed = _current_removed()
    removed.add(ticker)
    _write_store(lst, removed)
    _clear_caches()
    return {"removed": ticker, "sector": removed_from}


def remove_tickers(tickers: list[str]) -> dict:
    """Remove many constituents in one write (used by 'remove all dead'). Skips
    sector ETFs and names not in the universe; tombstones what it removes."""
    wanted = [str(t).strip().upper() for t in (tickers or []) if str(t).strip()]
    sectors = _load()
    etfs = set(sectors)
    lst = _sectors_as_list()
    removed, skipped = [], []
    to_remove = set()
    for t in wanted:
        if t in etfs:
            skipped.append({"ticker": t, "reason": "sector ETF"})
        else:
            to_remove.add(t)
    for s in lst:
        present = [t for t in s["tickers"] if t in to_remove]
        for t in present:
            s["tickers"].remove(t)
            removed.append(t)
    found = set(removed)
    skipped += [{"ticker": t, "reason": "not in universe"} for t in to_remove if t not in found]
    if removed:
        tomb = _current_removed()
        tomb.update(removed)
        _write_store(lst, tomb)
        _clear_caches()
    return {"removed": removed, "skipped": skipped}


def sync_from_seed() -> dict:
    """Additively pull in any names in the repo seed file that aren't in the store
    and aren't tombstoned (e.g. after new tickers/ETFs are added to the seed).
    The merge also runs automatically on load; this just forces it now and
    reports what it added. Never removes or moves anything."""
    store = _read_store()
    before = set()
    if store:
        for s in store["sectors"]:
            before.add(str(s.get("etf", "")).upper())
            before.update(str(t).upper() for t in s.get("tickers", []))
    _clear_caches()
    after = set(all_tickers())   # triggers _load -> seed merge -> write
    added = sorted(after - before)
    return {"synced": True, "added": added, "count": len(added)}


def reseed_from_file() -> dict:
    """Reset the volume store back to the baked-in repo file (discards runtime
    edits AND tombstones). Useful after fixing the seed file in a deploy."""
    raw = _seed_from_file()
    _write_store(raw, [])
    _clear_caches()
    return {"reseeded": True, "sectors": len(raw)}


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


def is_etf(ticker: str) -> bool:
    """True for a sector-ETF header (XLK…SPY) or a known tradeable ETF — used to
    put the name on the lower-juice ETF income sleeve at the entry gate."""
    t = (ticker or "").strip().upper()
    return t in _load() or t in config.KNOWN_ETFS
