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
ACCOUNTS_BASE = "https://api.schwabapi.com/trader/v1"

REFRESH_TOKEN_TTL_DAYS = 7
SYMBOL_MAP = {"^VIX": "$VIX", "^NYA": "$NYA", "^GSPC": "$SPX"}

_TOKEN_FILE = os.path.join(config.DATA_DIR, "schwab_token.json")
_token_lock = threading.Lock()


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
                 "Content-Type": "application/x-www-form-urlencoded"},
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
        "last": _n(q.get("lastPrice")),
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
                     "Content-Type": "application/x-www-form-urlencoded"},
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
        h = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
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

    def get_option_chain(self, symbol: str, expiry_date: str | None = None) -> dict:
        params = {"symbol": symbol.upper(), "contractType": "CALL", "strikeCount": 50}
        if expiry_date:
            params["expirationDate"] = expiry_date
        resp = requests.get(OPTION_CHAIN_URL, headers=self._auth_headers(), params=params, timeout=20)
        if resp.status_code != 200:
            raise SchwabError(f"schwab option chain: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

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
