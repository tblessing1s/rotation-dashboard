"""Schwab Trader API client — market data, quotes, option chains, and live
order execution + capture for CFM.

KEPT and adapted from the prior build's provider. Self-contained: the refresh
token persists to a small JSON file under DATA_DIR (written by the hosted OAuth
callback) and falls back to the SCHWAB_REFRESH_TOKEN env secret for bootstrap.
Schwab refresh tokens expire after 7 days and require a fresh browser login to
renew — there is no programmatic refresh.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pandas as pd
import requests

import config

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"
QUOTES_URL = "https://api.schwabapi.com/marketdata/v1/quotes"
OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
INSTRUMENTS_URL = "https://api.schwabapi.com/marketdata/v1/instruments"
ACCOUNTS_BASE = "https://api.schwabapi.com/trader/v1"

REFRESH_TOKEN_TTL_DAYS = 7
SYMBOL_MAP = {"^VIX": "$VIX", "^NYA": "$NYA", "^GSPC": "$SPX"}

# Schwab's market-data + trader hosts sit behind Akamai bot management, which
# returns an HTML "Access Denied" 403 for requests that don't look like they came
# from a browser — notably the default ``python-requests/x.y`` User-Agent from a
# cloud host IP. The OAuth token host is separate infra and isn't gated the same
# way, so a token can refresh cleanly while every data/chain call 403s. Sending a
# real browser User-Agent on every request clears the block. (The option-chain
# endpoint is the most sensitive because it has no local cache to fall back on.)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_TOKEN_FILE = os.path.join(config.DATA_DIR, "schwab_token.json")
_token_lock = threading.Lock()

# Short in-process cache for the accounts call (cash balance) — the Level 5
# gate can re-evaluate several times a minute while the operator tweaks a
# ticket, and this endpoint isn't part of the market-data rate-limit budget
# but there's no reason to hit it on every keystroke either.
_ACCOUNTS_TTL = 60  # seconds
_accounts_cache: tuple[float, list] | None = None
_accounts_lock = threading.Lock()


class SchwabError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Token store (JSON file under DATA_DIR)
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_token_file() -> dict:
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_token_file(data: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def store_refresh_token(refresh_token: str) -> None:
    with _token_lock:
        rec = _read_token_file()
        rec.update({"refresh_token": refresh_token, "minted_at": _utcnow(), "auth_error": None})
        _write_token_file(rec)


def current_refresh_token() -> str | None:
    rec = _read_token_file()
    return rec.get("refresh_token") or os.environ.get("SCHWAB_REFRESH_TOKEN")


def app_credentials() -> tuple[str, str]:
    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise SchwabError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")
    return key, secret


def configured() -> bool:
    return bool(
        os.environ.get("SCHWAB_APP_KEY")
        and os.environ.get("SCHWAB_APP_SECRET")
        and current_refresh_token()
    )


def token_status() -> dict:
    rec = _read_token_file()
    refresh = rec.get("refresh_token") or os.environ.get("SCHWAB_REFRESH_TOKEN")
    if not refresh:
        return {"present": False, "status": "missing"}
    out: dict = {"present": True, "source": "file" if rec.get("refresh_token") else "env"}
    minted_at = rec.get("minted_at")
    out["mintedAt"] = minted_at
    if not minted_at:
        out["status"] = "unknown"
        return out
    try:
        minted = datetime.strptime(minted_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        out["status"] = "unknown"
        return out
    expires = minted + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
    days_left = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    out["expiresAt"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["daysLeft"] = round(days_left, 2)
    out["status"] = "expired" if days_left <= 0 else "warning" if days_left <= 2 else "ok"
    return out


# ---------------------------------------------------------------------------
# OAuth (hosted re-auth flow)
# ---------------------------------------------------------------------------
def authorize_url(redirect_uri: str, state: str) -> str:
    client_id, _ = app_credentials()
    return AUTHORIZE_URL + "?" + urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
    )


def exchange_code(code: str, redirect_uri: str) -> dict:
    client_id, client_secret = app_credentials()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": USER_AGENT},
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        timeout=20,
    )
    if resp.status_code != 200:
        raise SchwabError(f"schwab code exchange failed (HTTP {resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _parse_quote_node(symbol: str, node: dict) -> dict:
    q = (node or {}).get("quote") or {}

    def _n(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    asset_type = (node or {}).get("assetMainType") or (node or {}).get("assetType")
    return {
        "symbol": symbol,
        "assetType": asset_type,
        # Index quotes ($VIX, $SPX) report under lastPrice intraday but only
        # closePrice off-hours, so expose close as well; callers fall back to it.
        "last": _n(q.get("lastPrice")),
        "close": _n(q.get("closePrice")),
        "bid": _n(q.get("bidPrice")),
        "ask": _n(q.get("askPrice")),
        "mark": _n(q.get("mark")),
        "underlyingPrice": _n(q.get("underlyingPrice")),
        "theta": _n(q.get("theta")),
        "delta": _n(q.get("delta")),
        "openInterest": _n(q.get("openInterest")),
        "quoteTimeMs": q.get("quoteTime"),
    }


class SchwabClient:
    """Live Schwab client. One instance is shared process-wide (see app.py)."""

    def __init__(self):
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # -- auth ----------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        client_id, client_secret = app_credentials()
        refresh = current_refresh_token()
        if not refresh:
            raise SchwabError("no schwab refresh token — re-authorize at /auth/schwab")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=20,
        )
        if resp.status_code != 200:
            with _token_lock:
                rec = _read_token_file()
                rec["auth_error"] = {"at": _utcnow(), "status": resp.status_code, "body": resp.text[:300]}
                _write_token_file(rec)
            raise SchwabError(
                f"schwab token refresh failed (HTTP {resp.status_code}) — "
                "refresh token likely expired; re-authorize at /auth/schwab"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 1800))
        return self._access_token

    def _auth_headers(self, extra: dict | None = None) -> dict:
        # User-Agent is required: Schwab's Akamai edge 403s a default requests UA
        # from a cloud host. Covers every market-data + trader call (quotes, price
        # history, chains, instruments, accounts, orders).
        h = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json",
             "User-Agent": USER_AGENT}
        if extra:
            h.update(extra)
        return h

    # -- market data ---------------------------------------------------------
    def get_daily_bars(self, symbol: str, start: str) -> pd.DataFrame:
        schwab_symbol = SYMBOL_MAP.get(symbol, symbol)
        start_ms = int(pd.Timestamp(start).timestamp() * 1000)
        resp = requests.get(
            PRICE_HISTORY_URL,
            headers=self._auth_headers(),
            params={"symbol": schwab_symbol, "periodType": "year", "frequencyType": "daily",
                    "frequency": 1, "startDate": start_ms, "needExtendedHoursData": "false"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SchwabError(f"schwab {symbol}: HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        candles = payload.get("candles") or []
        if payload.get("empty") or not candles:
            raise SchwabError(f"schwab {symbol}: empty response")
        idx = pd.to_datetime([c["datetime"] for c in candles], unit="ms", utc=True) \
            .tz_convert("America/New_York").normalize().tz_localize(None)
        df = pd.DataFrame({
            "Open": [c.get("open") for c in candles],
            "High": [c.get("high") for c in candles],
            "Low": [c.get("low") for c in candles],
            "Close": [c.get("close") for c in candles],
            "Volume": [c.get("volume") for c in candles],
        }, index=idx).dropna(subset=["Close"])
        if df.empty:
            raise SchwabError(f"schwab {symbol}: no usable rows")
        return df

    def get_quotes(self, symbols) -> dict:
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [s for s in symbols if s]
        if not symbols:
            return {}
        mapped = {s: SYMBOL_MAP.get(s, s) for s in symbols}
        resp = requests.get(
            QUOTES_URL,
            headers=self._auth_headers(),
            params={"symbols": ",".join(mapped.values()), "fields": "quote"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise SchwabError(f"schwab quotes: HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json() or {}
        by_norm = {str(k).replace(" ", ""): v for k, v in payload.items()}
        out = {}
        for orig, ms in mapped.items():
            node = payload.get(ms) or payload.get(orig) or by_norm.get(str(ms).replace(" ", ""))
            out[orig] = _parse_quote_node(orig, node) if node else None
        return out

    def get_quote(self, symbol: str) -> dict:
        parsed = self.get_quotes([symbol]).get(symbol)
        if not parsed:
            raise SchwabError(f"schwab quote {symbol}: no quote")
        return parsed

    def get_option_chain(self, symbol: str, expiry_date: str | None = None,
                         strike_count: int = 50, from_date: str | None = None,
                         to_date: str | None = None) -> dict:
        """Fetch the chain (calls + puts). With from_date/to_date and a larger
        strike_count the response spans both near-term (weekly short) and
        far-dated (LEAP) expirations in one call. Puts are included so an ITM
        call's delta can be recomputed off the same-strike put's (more reliable,
        skew-aware) IV. includeUnderlyingQuote pins the spot price."""
        params = {"symbol": symbol.upper(), "contractType": "ALL",
                  "strikeCount": strike_count, "includeUnderlyingQuote": "true"}
        if expiry_date:
            params["expirationDate"] = expiry_date
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        resp = requests.get(OPTION_CHAIN_URL, headers=self._auth_headers(), params=params, timeout=20)
        if resp.status_code == 403 and "Access Denied" in (resp.text or ""):
            # Akamai edge block (HTML body), distinct from an app-level 403. If it
            # persists with a browser User-Agent set, it points at the Schwab app's
            # market-data entitlement rather than the request itself.
            raise SchwabError(
                "schwab option chain: HTTP 403 blocked at the Schwab/Akamai edge — "
                "the request was denied before reaching the API. Confirm the Schwab "
                "app is approved for market data; a token refresh will not fix this.")
        if resp.status_code != 200:
            raise SchwabError(f"schwab option chain: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def get_instrument_fundamental(self, symbol: str) -> dict:
        """Fundamental block for one symbol (projection=fundamental). Carries the
        dividend yield (`divYield`, in percent) used to adjust call deltas."""
        resp = requests.get(
            INSTRUMENTS_URL, headers=self._auth_headers(),
            params={"symbol": symbol.upper(), "projection": "fundamental"}, timeout=20,
        )
        if resp.status_code != 200:
            raise SchwabError(f"schwab instruments: HTTP {resp.status_code} {resp.text[:200]}")
        instruments = (resp.json() or {}).get("instruments") or []
        return (instruments[0].get("fundamental") or {}) if instruments else {}

    # -- accounts & trading --------------------------------------------------
    _ACCT_HINT = (" — confirm the Schwab app is approved for 'Accounts and "
                  "Trading Production' and the refresh token is current")

    def _get_json(self, url: str, params: dict | None = None):
        resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        hint = self._ACCT_HINT if resp.status_code in (401, 403) else ""
        raise SchwabError(f"schwab account: HTTP {resp.status_code} {resp.text[:200]}{hint}")

    def account_numbers(self) -> list[dict]:
        return self._get_json(f"{ACCOUNTS_BASE}/accounts/accountNumbers") or []

    def primary_account_hash(self) -> str:
        nums = self.account_numbers()
        if not nums:
            raise SchwabError("schwab: no linked accounts")
        return nums[0].get("hashValue")

    def get_accounts(self, positions: bool = True) -> list[dict]:
        params = {"fields": "positions"} if positions else None
        return self._get_json(f"{ACCOUNTS_BASE}/accounts", params=params) or []

    def cash_balance(self, force: bool = False) -> float:
        """Tradable cash balance of the primary linked account (the same
        account order placement uses), briefly cached so repeated Level 5 gate
        checks don't hammer the endpoint. Raises SchwabError on failure —
        callers degrade to the stored manual value rather than block on this."""
        global _accounts_cache
        with _accounts_lock:
            now = time.time()
            if not force and _accounts_cache and now - _accounts_cache[0] < _ACCOUNTS_TTL:
                accounts = _accounts_cache[1]
            else:
                accounts = self.get_accounts(positions=False)
                _accounts_cache = (now, accounts)
        if not accounts:
            raise SchwabError("schwab: no linked accounts")
        cash = _account_cash(accounts[0])
        if cash is None:
            raise SchwabError("schwab: account response had no recognizable cash balance field")
        return cash

    def preview_order(self, account_hash: str, order: dict) -> dict:
        resp = requests.post(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/previewOrder",
            headers=self._auth_headers({"Content-Type": "application/json"}),
            json=order, timeout=30,
        )
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}
        hint = self._ACCT_HINT if resp.status_code in (401, 403) else ""
        raise SchwabError(f"schwab preview: HTTP {resp.status_code} {resp.text[:300]}{hint}")

    def place_order(self, account_hash: str, order: dict) -> dict:
        """Transmit a REAL order. Returns {orderId, location}. Caller gates this
        behind the live-trading enable flag (see executor.py)."""
        resp = requests.post(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders",
            headers=self._auth_headers({"Content-Type": "application/json"}),
            json=order, timeout=30,
        )
        if resp.status_code in (200, 201):
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
            return {"orderId": order_id, "location": location}
        hint = self._ACCT_HINT if resp.status_code in (401, 403) else ""
        raise SchwabError(f"schwab place order: HTTP {resp.status_code} {resp.text[:300]}{hint}")

    def get_order(self, account_hash: str, order_id: str) -> dict:
        return self._get_json(f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}") or {}

    def cancel_order(self, account_hash: str, order_id: str) -> dict:
        """Cancel a working order. Schwab returns 200/201 or an empty 204."""
        resp = requests.delete(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}",
            headers=self._auth_headers(), timeout=30,
        )
        if resp.status_code in (200, 201, 204):
            return {"orderId": order_id, "canceled": True}
        hint = self._ACCT_HINT if resp.status_code in (401, 403) else ""
        raise SchwabError(f"schwab cancel order: HTTP {resp.status_code} {resp.text[:200]}{hint}")


# ---------------------------------------------------------------------------
# Account parsing (module-level)
# ---------------------------------------------------------------------------
def _account_cash(node: dict) -> float | None:
    """Tradable cash from one /accounts response node. Tries the fields in
    order of how directly they represent 'money available to deploy right
    now' — cashAvailableForTrading (margin/cash accounts both report it) first,
    falling back to the raw cash balance for older/thin responses."""
    balances = ((node or {}).get("securitiesAccount") or {}).get("currentBalances") or {}
    for key in ("cashAvailableForTrading", "cashBalance", "availableFunds"):
        v = balances.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Order construction (module-level, provider-specific)
# ---------------------------------------------------------------------------
def occ_option_symbol(underlying: str, expiration: str, strike: float, call: bool = True) -> str:
    """Build the 21-char OCC option symbol Schwab expects in an order leg.

    Layout: 6-char root (left-justified, space-padded) + YYMMDD + C/P + strike×1000
    zero-padded to 8 digits. e.g. ('AAPL', '2024-09-20', 250, call) ->
    'AAPL  240920C00250000'. CFM trades calls, so `call` defaults True.
    """
    root = (underlying or "").strip().upper().ljust(6)
    y, m, d = str(expiration).split("-")
    yymmdd = f"{y[2:]}{int(m):02d}{int(d):02d}"
    cp = "C" if call else "P"
    strike_milli = int(round(float(strike) * 1000))
    return f"{root}{yymmdd}{cp}{strike_milli:08d}"


def build_single_leg_order(instruction: str, quantity: int, option_symbol: str,
                           limit_price: float) -> dict:
    """A single-leg DAY LIMIT option order in Schwab's order schema. `instruction`
    is one of BUY_TO_OPEN / SELL_TO_OPEN / BUY_TO_CLOSE / SELL_TO_CLOSE."""
    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "price": f"{float(limit_price):.2f}",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity": int(quantity),
            "instrument": {"symbol": option_symbol, "assetType": "OPTION"},
        }],
    }


def build_net_order(legs: list[tuple], net_price: float) -> dict:
    """A single multi-leg NET_CREDIT/NET_DEBIT DAY order so several option legs
    fill together or not at all — no legging risk. ``legs`` is a list of
    (instruction, option_symbol, quantity). ``net_price`` is per share: positive
    = net credit received, negative = net debit paid. Used for atomic exits
    (sell-to-close the LEAP + buy-to-close the short) and atomic LEAP rolls."""
    credit = float(net_price) >= 0
    return {
        "orderType": "NET_CREDIT" if credit else "NET_DEBIT",
        "session": "NORMAL",
        "price": f"{abs(float(net_price)):.2f}",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "orderLegCollection": [
            {"instruction": instr, "quantity": int(qty),
             "instrument": {"symbol": sym, "assetType": "OPTION"}}
            for instr, sym, qty in legs
        ],
    }


def build_roll_order(quantity: int, buy_to_close_symbol: str, sell_to_open_symbol: str,
                     net_price: float) -> dict:
    """A single two-leg NET_CREDIT/NET_DEBIT DAY order for a short-call roll:
    buy-to-close the old short + sell-to-open the new one on ONE ticket, so the
    roll cannot leg out (fill one side, miss the other). `net_price` is per
    share: positive = credit received, negative = debit paid. CUSTOM covers any
    strike/expiration combination (vertical or diagonal)."""
    credit = float(net_price) >= 0
    return {
        "orderType": "NET_CREDIT" if credit else "NET_DEBIT",
        "session": "NORMAL",
        "price": f"{abs(float(net_price)):.2f}",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": int(quantity),
                "instrument": {"symbol": buy_to_close_symbol, "assetType": "OPTION"},
            },
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": int(quantity),
                "instrument": {"symbol": sell_to_open_symbol, "assetType": "OPTION"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Chain parsing (module-level, provider-specific -> normalized dicts)
# ---------------------------------------------------------------------------
def _num(v):
    """Coerce a Schwab numeric field to a clean float, dropping None/NaN/non-numeric
    (Schwab sends 'NaN' deltas for far-dated strikes when the market is closed)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN != NaN


def parse_call_chain(payload: dict) -> tuple[float | None, list[dict]]:
    """Flatten Schwab's callExpDateMap into (underlying_price, normalized calls).

    Each normalized contract is a plain dict — strike, expiration (YYYY-MM-DD),
    dte, bid, ask, mark, last, delta, theta, open_interest, symbol — so the
    indicator helpers and the JSON API stay provider-agnostic.
    """
    underlying = _num(payload.get("underlyingPrice"))
    if underlying is None:
        u = payload.get("underlying") or {}
        underlying = _num(u.get("last")) or _num(u.get("mark"))

    contracts: list[dict] = []
    for exp_key, strikes in (payload.get("callExpDateMap") or {}).items():
        # exp_key looks like "2025-12-19:178" (expiration date : days-to-expiry).
        date_part = exp_key.split(":")[0]
        for strike_str, rows in (strikes or {}).items():
            for row in rows or []:
                contracts.append({
                    "symbol": row.get("symbol"),
                    "strike": _num(row.get("strikePrice")) or _num(strike_str),
                    "expiration": date_part,
                    "dte": row.get("daysToExpiration"),
                    "bid": _num(row.get("bid")),
                    "ask": _num(row.get("ask")),
                    "mark": _num(row.get("mark")),
                    "last": _num(row.get("last")),
                    "delta": _num(row.get("delta")),
                    "theta": _num(row.get("theta")),
                    "volatility": _num(row.get("volatility")),  # annualized IV %
                    "open_interest": row.get("openInterest"),
                })
    return underlying, contracts


def parse_put_iv(payload: dict) -> dict[tuple[str, float], float]:
    """Map (expiration YYYY-MM-DD, strike) -> put IV (%) from putExpDateMap.

    Same-strike calls and puts share one implied vol, but for an ITM call the
    OTM put's IV is the stable, skew-aware value (the call's own IV collapses on
    thin time value). Callers use this to recompute ITM-call deltas the way TOS
    does."""
    out: dict[tuple[str, float], float] = {}
    for exp_key, strikes in (payload.get("putExpDateMap") or {}).items():
        date_part = exp_key.split(":")[0]
        for strike_str, rows in (strikes or {}).items():
            for row in rows or []:
                strike = _num(row.get("strikePrice")) or _num(strike_str)
                iv = _num(row.get("volatility"))
                if strike is not None and iv is not None:
                    out[(date_part, strike)] = iv
    return out


def parse_put_quotes(payload: dict) -> dict[tuple[str, float], dict]:
    """Map (expiration YYYY-MM-DD, strike) -> {bid, ask, mark} from putExpDateMap.

    Lets a caller imply a skew-aware vol from the OTM put's *price* when the
    provider's IV field is missing (e.g. off-hours NaNs) — the put carries time
    value, so its mark implies a usable vol that recovers the ITM call's delta."""
    out: dict[tuple[str, float], dict] = {}
    for exp_key, strikes in (payload.get("putExpDateMap") or {}).items():
        date_part = exp_key.split(":")[0]
        for strike_str, rows in (strikes or {}).items():
            for row in rows or []:
                strike = _num(row.get("strikePrice")) or _num(strike_str)
                if strike is None:
                    continue
                bid, ask, mark = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("mark"))
                if mark is None and bid is not None and ask is not None:
                    mark = round((bid + ask) / 2, 4)
                out[(date_part, strike)] = {"bid": bid, "ask": ask, "mark": mark}
    return out
