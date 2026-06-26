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
OPTION_CHAIN_URL = "https://api.schwabapi.com/marketdata/v1/chains"
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


def _parse_quote_node(symbol: str, node: dict) -> dict:
    """Flatten one Schwab quote node into the fields the dashboard uses.

    Equity nodes give last/bid/ask/mark. Option nodes add ``underlyingPrice``
    (the stock price at the same instant) and greeks (``theta``/``delta``), so
    an option quote alone is enough to re-decompose the premium.
    """
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
        "raw": node,
    }


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

    def get_quotes(self, symbols) -> dict:
        """Real-time quotes for many symbols in ONE call (equities AND options).

        Schwab's quotes endpoint accepts a comma-joined symbol list, so a whole
        theta ledger refreshes in a single request — important for staying under
        the API budget. For an OPTION symbol the quote node also carries
        ``underlyingPrice`` and the greeks, so one option quote yields everything
        needed to re-split the premium (no separate stock-quote call per leg).

        Returns ``{requested_symbol: parsed_or_None}``. Missing symbols map to
        None rather than raising, so one bad symbol never sinks the batch.
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [s for s in symbols if s]
        if not symbols:
            return {}
        mapped = {s: SYMBOL_MAP.get(s, s) for s in symbols}
        try:
            resp = requests.get(
                QUOTES_URL,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                params={"symbols": ",".join(mapped.values()), "fields": "quote"},
                timeout=20,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab quotes: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"schwab quotes: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderError("schwab quotes: non-JSON response") from e
        # Match response keys tolerantly: OSI option symbols carry internal
        # padding spaces, so key the payload by a space-stripped form too.
        by_norm = {str(k).replace(" ", ""): v for k, v in (payload or {}).items()}
        out = {}
        for orig, ms in mapped.items():
            node = payload.get(ms) or payload.get(orig) or by_norm.get(str(ms).replace(" ", ""))
            out[orig] = _parse_quote_node(orig, node) if node else None
        return out

    def get_quote(self, symbol: str) -> dict:
        """Real-time quote for one symbol (thin wrapper over get_quotes).

        Used to snapshot the underlying's price at option-execution time so a
        fill can be split into intrinsic/extrinsic against the price at that
        instant. Raises if the symbol has no quote.
        """
        parsed = self.get_quotes([symbol]).get(symbol)
        if not parsed:
            raise ProviderError(f"schwab quote {symbol}: no quote in response")
        return parsed

    def get_option_chain(self, symbol: str, expiry_date: str | None = None) -> dict:
        """Fetch option chain for a symbol, optionally filtered to one expiry.

        Returns structured chain data: {expirations: [...], callExpDateMap, putExpDateMap}.
        Each leg maps to {bid, ask, mark, impliedVolatility, theta, ...}.
        If expiry_date is specified, only that expiry's legs are returned.
        """
        try:
            params = {"symbol": symbol.upper(), "contractType": "ALL", "strikeCount": 100}
            if expiry_date:
                params["expirationDate"] = expiry_date
            resp = requests.get(
                OPTION_CHAIN_URL,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                params=params,
                timeout=20,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab option chain: {e}") from e
        if resp.status_code != 200:
            raise ProviderError(f"schwab option chain: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError("schwab option chain: non-JSON response") from e

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

    def cancel_order(self, account_hash: str, order_id: str) -> dict:
        """Cancel a working order (DELETE .../orders/{id}).

        Risk-reducing — it closes exposure rather than opening it — so callers
        do not gate this behind the live-trading kill-switch. Schwab returns an
        empty 200/201 on success; a 400 typically means the order already
        filled or was already canceled (no longer cancelable).
        """
        url = f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}"
        try:
            resp = requests.delete(
                url,
                headers={"Authorization": f"Bearer {self._token()}", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise ProviderError(f"schwab cancel order request failed: {e}") from e
        if resp.status_code in (200, 201):
            return {"orderId": str(order_id), "canceled": True}
        hint = self._ACCOUNT_ACCESS_HINT if resp.status_code in (401, 403) else ""
        raise ProviderError(f"schwab cancel order: HTTP {resp.status_code} {resp.text[:300]}{hint}")

    def replace_order(self, account_hash: str, order_id: str, order: dict) -> dict:
        """Atomically cancel `order_id` and submit `order` in its place (PUT).

        This is the broker-native "work the order" path: re-pricing a resting
        limit without a naked moment between a separate cancel and a new place.
        Schwab cancels the original and mints a NEW order id (returned in the
        Location header), so the caller polls the new id for the fill.
        """
        url = f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}"
        try:
            resp = requests.put(
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
            raise ProviderError(f"schwab replace order request failed: {e}") from e
        if resp.status_code in (200, 201):
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            new_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
            return {"orderId": new_id, "replacedOrderId": str(order_id), "location": location}
        hint = self._ACCOUNT_ACCESS_HINT if resp.status_code in (401, 403) else ""
        raise ProviderError(f"schwab replace order: HTTP {resp.status_code} {resp.text[:300]}{hint}")
