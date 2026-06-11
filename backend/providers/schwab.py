from __future__ import annotations

import base64
import os
import time

import pandas as pd
import requests

import db

from .base import Provider, ProviderError

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
PRICE_HISTORY_URL = "https://api.schwabapi.com/marketdata/v1/pricehistory"

# Schwab prefixes index symbols with $ where Yahoo uses ^.
SYMBOL_MAP = {"^VIX": "$VIX", "^NYA": "$NYA", "^GSPC": "$SPX"}


class SchwabProvider(Provider):
    """Schwab Trader API — matches thinkorswim's data feed.

    Credentials come from env (Fly secrets): SCHWAB_APP_KEY, SCHWAB_APP_SECRET,
    SCHWAB_REFRESH_TOKEN. Schwab refresh tokens expire after 7 days; when the
    refresh fails the error is recorded (surfaced in the data-issues panel)
    and the chain falls through to the next provider. Re-mint with:
    `python cli.py schwab-auth`.
    """

    name = "schwab"

    def __init__(self):
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def configured() -> bool:
        return all(
            os.environ.get(k)
            for k in ("SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_REFRESH_TOKEN")
        )

    # -- auth ----------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        key = os.environ["SCHWAB_APP_KEY"]
        secret = os.environ["SCHWAB_APP_SECRET"]
        refresh = os.environ["SCHWAB_REFRESH_TOKEN"]
        basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
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
                "refresh token likely expired; run `python cli.py schwab-auth`"
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
