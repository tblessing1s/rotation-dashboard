from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pandas as pd
import requests

import db

from .base import Provider, ProviderError

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"
QUOTES_URL = "https://api.schwabapi.com/marketdata/v1/quotes"
ACCOUNTS_BASE = "https://api.schwabapi.com/trader/v1"

# Schwab refresh tokens are valid for exactly 7 days after they are minted via
# the authorization_code flow, and there is no way to extend one programmatically
# (Schwab requires a fresh browser login). We track the mint time so the UI can
# warn before it lapses. See backend/app.py for the hosted re-auth endpoints.
REFRESH_TOKEN_TTL_DAYS = 7

# Schwab prefixes index symbols with $ where Yahoo uses ^.
SYMBOL_MAP = {"^VIX": "$VIX", "^NYA": "$NYA", "^GSPC": "$SPX"}


# ---------------------------------------------------------------------------
# Credentials & OAuth (shared by the CLI bootstrap and the hosted re-auth flow)
# ---------------------------------------------------------------------------
def app_credentials() -> tuple[str, str]:
    """App key/secret (a.k.a. Schwab Client ID / Client Secret) from env."""
    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise ProviderError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")
    return key, secret


def current_refresh_token() -> str | None:
    """The active refresh token: the DB-stored one (set by the hosted re-auth
    flow) wins over the bootstrap env secret, since it is always the freshest."""
    rec = db.kv_get("schwab_token") or {}
    return rec.get("refresh_token") or os.environ.get("SCHWAB_REFRESH_TOKEN")


def store_refresh_token(refresh_token: str) -> None:
    """Persist a freshly minted refresh token (and its mint time) to the
    datastore, and clear any prior auth error."""
    db.kv_set("schwab_token", {"refresh_token": refresh_token, "minted_at": db.utcnow()})
    db.kv_set("schwab_auth_error", None)


def authorize_url(redirect_uri: str, state: str) -> str:
    """The Schwab consent URL the user opens in a browser to grant access."""
    client_id, _ = app_credentials()
    return AUTHORIZE_URL + "?" + urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
    )


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for tokens (authorization_code grant).

    `redirect_uri` must exactly match the callback registered with the app and
    used in the authorize request. Returns the raw token payload.
    """
    client_id, client_secret = app_credentials()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            timeout=20,
        )
    except requests.RequestException as e:
        raise ProviderError(f"schwab code exchange request failed: {e}") from e
    if resp.status_code != 200:
        raise ProviderError(
            f"schwab code exchange failed (HTTP {resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()


def token_status() -> dict:
    """Health of the refresh token for the data-issues panel.

    status is one of: missing, unknown (env token, mint time not tracked),
    ok, warning (<=2 days left), expired.
    """
    rec = db.kv_get("schwab_token") or {}
    refresh = rec.get("refresh_token") or os.environ.get("SCHWAB_REFRESH_TOKEN")
    if not refresh:
        return {"present": False, "status": "missing"}
    out: dict = {"present": True, "source": "db" if rec.get("refresh_token") else "env"}
    minted_at = rec.get("minted_at")
    out["mintedAt"] = minted_at
    if not minted_at:
        # An env-provided token has no tracked mint time, so we cannot age it.
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


class SchwabProvider(Provider):
    """Schwab Trader API — matches thinkorswim's data feed.

    App credentials come from env (Fly secrets): SCHWAB_APP_KEY,
    SCHWAB_APP_SECRET. The refresh token is read from the datastore first
    (refreshed via the hosted re-auth page, /auth/schwab) and falls back to the
    SCHWAB_REFRESH_TOKEN env secret for bootstrap. Schwab refresh tokens expire
    after 7 days; when the refresh fails the error is recorded (surfaced in the
    data-issues panel) and the chain falls through to the next provider.
    Re-mint by visiting /auth/schwab or running `python cli.py schwab-auth`.
    """

    name = "schwab"

    def __init__(self):
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def configured() -> bool:
        return bool(
            os.environ.get("SCHWAB_APP_KEY")
            and os.environ.get("SCHWAB_APP_SECRET")
            and current_refresh_token()
        )

    # -- auth ----------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        client_id, client_secret = app_credentials()
        refresh = current_refresh_token()
        if not refresh:
            raise ProviderError("no schwab refresh token — re-authorize at /auth/schwab")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        try:
            resp = requests.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "refresh_token", "refresh_token": refresh},
                timeout=20,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab token request failed: {e}") from e
        if resp.status_code != 200:
            # Most likely the 7-day refresh token expired.
            db.kv_set(
                "schwab_auth_error",
                {"at": db.utcnow(), "status": resp.status_code, "body": resp.text[:300]},
            )
            raise ProviderError(
                f"schwab token refresh failed (HTTP {resp.status_code}) — "
                "refresh token likely expired; re-authorize at /auth/schwab"
            )
        db.kv_set("schwab_auth_error", None)
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 1800))
        return self._access_token

    # -- data ----------------------------------------------------------------
    def get_daily_bars(self, symbol: str, start: str) -> pd.DataFrame:
        schwab_symbol = SYMBOL_MAP.get(symbol, symbol)
        start_ms = int(pd.Timestamp(start).timestamp() * 1000)
        try:
            resp = requests.get(
                PRICE_HISTORY_URL,
                headers={"Authorization": f"Bearer {self._token()}"},
                params={
                    "symbol": schwab_symbol,
                    "periodType": "year",
                    "frequencyType": "daily",
                    "frequency": 1,
                    "startDate": start_ms,
                    "needExtendedHoursData": "false",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab {symbol}: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"schwab {symbol}: HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        candles = payload.get("candles") or []
        if payload.get("empty") or not candles:
            raise ProviderError(f"schwab {symbol}: empty response")
        idx = pd.to_datetime([c["datetime"] for c in candles], unit="ms", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
        df = pd.DataFrame(
            {
                "Open": [c.get("open") for c in candles],
                "High": [c.get("high") for c in candles],
                "Low": [c.get("low") for c in candles],
                "Close": [c.get("close") for c in candles],
                "Volume": [c.get("volume") for c in candles],
            },
            index=idx,
        ).dropna(subset=["Close"])
        if df.empty:
            raise ProviderError(f"schwab {symbol}: no usable rows")
        return df

    def get_intraday_bars(self, symbol: str, start: str, end: str,
                          interval_min: int = 5, extended_hours: bool = False) -> pd.DataFrame:
        """5-minute (or `interval_min`) bars from Schwab for the backtester.

        Uses the same pricehistory endpoint as the daily feed but with a minute
        frequency. `end` is inclusive: Schwab's endDate is the start of the last
        candle, so we push it to the end of that ET day.
        """
        schwab_symbol = SYMBOL_MAP.get(symbol, symbol)
        start_ms = int(pd.Timestamp(start).timestamp() * 1000)
        # Make `end` inclusive of the whole ET trading day.
        end_ms = int((pd.Timestamp(end) + pd.Timedelta(days=1)).timestamp() * 1000)
        try:
            resp = requests.get(
                PRICE_HISTORY_URL,
                headers={"Authorization": f"Bearer {self._token()}"},
                params={
                    "symbol": schwab_symbol,
                    "periodType": "day",
                    "frequencyType": "minute",
                    "frequency": interval_min,
                    "startDate": start_ms,
                    "endDate": end_ms,
                    "needExtendedHoursData": "true" if extended_hours else "false",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab {symbol} intraday: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"schwab {symbol} intraday: HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        candles = payload.get("candles") or []
        if payload.get("empty") or not candles:
            raise ProviderError(f"schwab {symbol} intraday: empty response")
        idx = pd.to_datetime([c["datetime"] for c in candles], unit="ms", utc=True) \
            .tz_convert("America/New_York")
        df = pd.DataFrame(
            {
                "Open": [c.get("open") for c in candles],
                "High": [c.get("high") for c in candles],
                "Low": [c.get("low") for c in candles],
                "Close": [c.get("close") for c in candles],
                "Volume": [c.get("volume") for c in candles],
            },
            index=idx,
        ).dropna(subset=["Close"])
        if df.empty:
            raise ProviderError(f"schwab {symbol} intraday: no usable rows")
        return df

    def get_quote(self, symbol: str) -> dict:
        """Real-time quote for one symbol from the market-data feed.

        Returns the live last/bid/ask/mark plus Schwab's quoteTime (epoch ms),
        normalized into a flat dict. This is the same market-data product the
        bar feeds use (no extra Schwab approval needed), and it is the price
        snapshot taken at option-execution time so an option fill can be split
        into intrinsic/extrinsic against the underlying's price at that instant.
        """
        schwab_symbol = SYMBOL_MAP.get(symbol, symbol)
        try:
            resp = requests.get(
                QUOTES_URL,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                params={"symbols": schwab_symbol, "fields": "quote"},
                timeout=20,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab quote {symbol}: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"schwab quote {symbol}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError(f"schwab quote {symbol}: non-JSON response") from e
        node = payload.get(schwab_symbol) or payload.get(symbol)
        if not node:
            raise ProviderError(f"schwab quote {symbol}: no quote in response")
        q = node.get("quote") or {}

        def _n(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        last = _n(q.get("lastPrice"))
        bid = _n(q.get("bidPrice"))
        ask = _n(q.get("askPrice"))
        mark = _n(q.get("mark"))
        return {
            "symbol": symbol,
            "last": last,
            "bid": bid,
            "ask": ask,
            "mark": mark,
            "quoteTimeMs": q.get("quoteTime"),
            "raw": node,
        }

    # -- accounts & trading --------------------------------------------------
    # These hit the Trader API's account endpoints (a different product than the
    # market-data feed above). The same app key / secret / refresh token are
    # used, but the Schwab app must also be approved for the "Accounts and
    # Trading Production" product or these return HTTP 401/403.
    _ACCOUNT_ACCESS_HINT = (
        " — the token lacks account access; confirm the Schwab app is "
        "approved for 'Accounts and Trading Production' and the refresh "
        "token is current (`python cli.py schwab-auth`)"
    )

    def _get_json(self, url: str, params: dict | None = None):
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                params=params,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab account request failed: {e}") from e
        if resp.status_code == 200:
            return resp.json()
        hint = self._ACCOUNT_ACCESS_HINT if resp.status_code in (401, 403) else ""
        raise ProviderError(f"schwab account: HTTP {resp.status_code} {resp.text[:200]}{hint}")

    def _post_json(self, url: str, payload: dict):
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab order request failed: {e}") from e
        if resp.status_code in (200, 201):
            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        hint = self._ACCOUNT_ACCESS_HINT if resp.status_code in (401, 403) else ""
        raise ProviderError(f"schwab order: HTTP {resp.status_code} {resp.text[:300]}{hint}")

    def account_numbers(self) -> list[dict]:
        """Plain account number -> encrypted hashValue used by other endpoints."""
        return self._get_json(f"{ACCOUNTS_BASE}/accounts/accountNumbers") or []

    def get_accounts(self, positions: bool = True) -> list[dict]:
        """All linked accounts with balances; include current positions when asked."""
        params = {"fields": "positions"} if positions else None
        return self._get_json(f"{ACCOUNTS_BASE}/accounts", params=params) or []

    def get_transactions(self, account_hash: str, start: str, end: str, types: str = "TRADE") -> list[dict]:
        """Activity for one account between two ISO-8601 instants (max 1y span)."""
        return self._get_json(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/transactions",
            params={"startDate": start, "endDate": end, "types": types},
        ) or []

    def preview_order(self, account_hash: str, order: dict) -> dict:
        """Dry-run an order: validate it against the account WITHOUT placing it.

        Schwab has no paper-trading environment, so previewOrder is the closest
        safe equivalent — it returns the projected order value, commission/fees,
        and any validation rejects/alerts, but never fills. A real fill would use
        a (deliberately unimplemented here) POST to .../orders instead.
        """
        return self._post_json(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/previewOrder", order
        ) or {}

    def place_order(self, account_hash: str, order: dict) -> dict:
        """Transmit a REAL order to the live brokerage account (POST .../orders).

        This actually fills against real money — unlike preview_order, nothing
        is simulated. Schwab returns HTTP 201 with an empty body and the new
        order's URL in the Location header; the trailing path segment is the
        order id, which the caller polls via get_order to read the fill.

        Callers must gate this behind their own kill-switch (see
        option_trades.place_option): this method itself does not check any
        enable flag, so it can be unit-tested without a live account.
        """
        url = f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders"
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=order,
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab place order request failed: {e}") from e
        if resp.status_code in (200, 201):
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
            return {"orderId": order_id, "location": location}
        hint = self._ACCOUNT_ACCESS_HINT if resp.status_code in (401, 403) else ""
        raise ProviderError(f"schwab place order: HTTP {resp.status_code} {resp.text[:300]}{hint}")

    def get_order(self, account_hash: str, order_id: str) -> dict:
        """Fetch one order's current status + execution detail (for fill polling)."""
        return self._get_json(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}"
        ) or {}
