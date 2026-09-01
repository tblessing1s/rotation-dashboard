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
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pandas as pd
import requests

import config
import fetch_budget
import order_pricing
from decimal import Decimal as _Decimal


def _dec_ge_zero(value) -> bool:
    """True when a price (Decimal or number) is >= 0, without float wobble."""
    return _Decimal(str(value)) >= 0

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

# A CONNECTION is one OAuth grant. The deployment has one by default (the shared
# grant in schwab_token.json) and every book authenticates with it; a book whose
# brokerage account lives under a different Schwab login holds its own instead,
# in schwab_token.account-<id>.json. Which one a call uses follows the ACTIVE
# ACCOUNT, exactly as the state file does — see accounts.connection_id().
SHARED_CONNECTION = "shared"

# Short in-process cache for the accounts call (cash balance) — the Level 5
# gate can re-evaluate several times a minute while the operator tweaks a
# ticket, and this endpoint isn't part of the market-data rate-limit budget
# but there's no reason to hit it on every keystroke either.
_ACCOUNTS_TTL = 60  # seconds
_accounts_cache: tuple[float, list] | None = None
_accounts_lock = threading.Lock()


logger = logging.getLogger(__name__)


class SchwabError(RuntimeError):
    pass


# HTTP statuses worth retrying: rate-limit (429) + transient server/gateway
# errors. Any OTHER 4xx (401 re-auth, 403 entitlement, 404) is a durable error a
# retry won't fix, so it falls straight through to the caller's status check.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _request(method: str, url: str, *, sleep=time.sleep, **kwargs):
    """Issue an HTTP request with bounded exponential backoff on TRANSIENT
    failures — connection resets / read timeouts (which ``requests`` raises) and
    the retryable HTTP statuses above (429 + 5xx). Honors a ``Retry-After`` header
    when the server sends one, else backs off exponentially for a bounded number
    of attempts. ``sleep`` is injectable for tests.

    HOW LONG IT MAY TRY IS THE CALLER'S TO DECIDE, not this function's. The
    budget comes from ``fetch_budget.current()``: the background default is the
    ``SCHWAB_*`` knobs unchanged (~87s for one symbol, correct when nobody is
    waiting), while an HTTP request runs under the interactive budget — fewer
    attempts, a capped timeout, and a whole-request deadline past which no
    further attempt is made at all. These used to be one set of knobs
    deliberately, "so on-demand and background fetches behave alike"; that is
    exactly what hung the dashboard behind a dead provider. See fetch_budget.py.

    Read/idempotent calls only. Order *submission* (``place_order`` /
    ``submit_order``) is deliberately NOT routed through here: a retried POST could
    double-submit a live order, so that path keeps its own single-attempt,
    structured-outcome handling.

    Returns the final ``requests.Response`` (retryable or not on the last
    attempt); the caller does its own status check + ``SchwabError`` raising, so
    error messages and parsing stay exactly as before.
    """
    http_fn = getattr(requests, method.lower())
    budget = fetch_budget.current()
    delay = budget.base_seconds
    attempts = budget.attempts
    # Narrow (never widen) the call site's own timeout to fit the budget, so a
    # single attempt cannot outlive the request that is waiting on it.
    if "timeout" in kwargs or budget.timeout is not None:
        kwargs["timeout"] = budget.cap_timeout(kwargs.get("timeout"))
    for attempt in range(attempts):
        last_attempt = attempt >= attempts - 1
        # Out of time: stop retrying and let the caller degrade to cache. Only an
        # interactive budget carries a deadline, so this never fires in the
        # background.
        if attempt and budget.expired():
            # Raise rather than return: every caller does `resp.status_code`, so
            # a None here would surface as an AttributeError instead of the
            # thing that actually happened. As a SchwabError it lands in
            # `data_handler._fetch`'s except, which degrades to the cached frame
            # and records the reason for /api/data-health.
            raise SchwabError(
                f"request deadline reached after {attempt} attempt(s) "
                f"({method.upper()} {url}) — serving cached data instead")
        try:
            resp = http_fn(url, **kwargs)
        except requests.exceptions.RequestException as e:
            if last_attempt:
                raise
            wait = budget.sleep_for(min(delay, budget.max_seconds))
            logger.warning("schwab %s %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                           method.upper(), url, e.__class__.__name__, wait,
                           attempt + 1, attempts)
            sleep(wait)
            delay = min(delay * 2, budget.max_seconds)
            continue
        if resp.status_code in _RETRYABLE_STATUS and not last_attempt:
            retry_after = getattr(resp, "headers", {}).get("Retry-After")
            try:
                ra = float(retry_after) if retry_after else None
            except (TypeError, ValueError):
                ra = None
            wait = budget.sleep_for(
                min(ra if ra is not None else delay, budget.max_seconds))
            logger.warning("schwab %s %s HTTP %s; backing off %.1fs (attempt %d/%d)",
                           method.upper(), url, resp.status_code, wait,
                           attempt + 1, attempts)
            sleep(wait)
            delay = min(delay * 2, budget.max_seconds)
            continue
        return resp
    # Unreachable: the loop runs at least once and every path through it either
    # returns, raises, or continues. Kept explicit so a future edit that adds a
    # `break` fails loudly here rather than returning None into `.status_code`.
    raise SchwabError(f"no attempt was made ({method.upper()} {url})")


# ---------------------------------------------------------------------------
# Token store (JSON file under DATA_DIR)
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_connection() -> str:
    """The grant this call authenticates with — the active account's.

    Resolved through the registry (never cached) so it follows the request/job
    account binding, like config.active_state_path(). Degrades to the shared
    grant if the registry can't be read: one login still beats no login.
    """
    try:
        import accounts
        return accounts.connection_id()
    except Exception as e:  # noqa: BLE001 — a registry problem must not lock us out
        logger.warning("could not resolve the Schwab connection (%s); using the "
                       "shared one", e)
        return SHARED_CONNECTION


def market_connection() -> str:
    """The grant to use for MARKET DATA — bars, quotes, chains, fundamentals.

    Prices are the same whichever login asks, so a book whose own grant isn't
    connected (or has expired) still gets market data through the shared one
    rather than blanking its charts. This fallback is deliberately NOT available
    to account calls: an order, a cash read or a reconciliation answered by the
    wrong login is a correctness failure, a chart answered by either login is not.
    """
    connection = active_connection()
    if connection != SHARED_CONNECTION and not _grant_present(connection):
        return SHARED_CONNECTION
    return connection


def market_configured() -> bool:
    """Whether market data can be fetched from Schwab at all (see above).

    Asks ``configured()`` with no argument first — the active book's own grant,
    and the one call the suite stubs — then falls back to the shared login, so a
    book waiting on its own consent still draws charts.
    """
    if configured():
        return True
    return _grant_present(SHARED_CONNECTION)


def token_path(connection: str | None = None) -> str:
    """Where one connection's refresh token lives. The shared grant keeps the
    original ``schwab_token.json`` path, so an existing deployment's token is
    already the shared connection — nothing to move."""
    connection = connection or active_connection()
    base = os.path.join(config.DATA_DIR, "schwab_token.json")
    if connection == SHARED_CONNECTION:
        return base
    root, ext = os.path.splitext(base)
    return f"{root}.{connection}{ext}"


def _read_token_file(connection: str | None = None) -> dict:
    try:
        with open(token_path(connection), encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_token_file(data: dict, connection: str | None = None) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(token_path(connection), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def store_refresh_token(refresh_token: str, connection: str | None = None) -> None:
    with _token_lock:
        rec = _read_token_file(connection)
        rec.update({"refresh_token": refresh_token, "minted_at": _utcnow(), "auth_error": None})
        _write_token_file(rec, connection)


def current_refresh_token(connection: str | None = None) -> str | None:
    """This connection's refresh token.

    The SCHWAB_REFRESH_TOKEN env fallback belongs to the shared grant only: it is
    a deployment-level credential for the deployment's own login, and handing it
    to a book that authenticates separately would silently trade the wrong login's
    accounts — the exact failure a separate connection exists to prevent.
    """
    connection = connection or active_connection()
    rec = _read_token_file(connection)
    token = rec.get("refresh_token")
    if token:
        return token
    return os.environ.get("SCHWAB_REFRESH_TOKEN") if connection == SHARED_CONNECTION else None


def app_credentials() -> tuple[str, str]:
    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise SchwabError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")
    return key, secret


def _grant_present(connection: str | None = None) -> bool:
    """The raw check behind ``configured`` — app credentials plus a refresh token
    for this connection. Kept separate because ``configured`` is a seam the suite
    substitutes wholesale; internal resolution must not depend on the stub."""
    return bool(
        os.environ.get("SCHWAB_APP_KEY")
        and os.environ.get("SCHWAB_APP_SECRET")
        and current_refresh_token(connection)
    )


def configured(connection: str | None = None) -> bool:
    """Whether THIS book can reach Schwab: app credentials (deployment-wide, one
    app) plus a grant for its connection."""
    return _grant_present(connection)


def token_status(connection: str | None = None) -> dict:
    connection = connection or active_connection()
    rec = _read_token_file(connection)
    refresh = rec.get("refresh_token") or (
        os.environ.get("SCHWAB_REFRESH_TOKEN") if connection == SHARED_CONNECTION else None)
    if not refresh:
        return {"present": False, "status": "missing", "connection": connection}
    out: dict = {"present": True, "source": "file" if rec.get("refresh_token") else "env",
                 "connection": connection}
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
    """Live Schwab client, PINNED to one OAuth connection.

    One instance per connection is cached process-wide (data_handler.client()).
    The pin matters: the access token cached on the instance belongs to one grant,
    so a client that resolved its connection lazily could hand book B a token
    minted for book A's login — and Schwab would happily answer with A's accounts.
    """

    def __init__(self, connection: str | None = None):
        # None means "the shared grant" for a directly-constructed client; the
        # cached-per-connection factory always passes one explicitly.
        self.connection = connection or SHARED_CONNECTION
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # -- auth ----------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        client_id, client_secret = app_credentials()
        refresh = current_refresh_token(self.connection)
        if not refresh:
            owner = None
            try:
                import accounts
                owner = accounts.connection_owner(self.connection)
            except Exception:  # noqa: BLE001
                owner = None
            where = f"/auth/schwab?account={owner}" if owner else "/auth/schwab"
            raise SchwabError(
                f"no schwab refresh token for this account — connect it at {where}")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = _request(
            "post", TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            timeout=20,
        )
        if resp.status_code != 200:
            with _token_lock:
                rec = _read_token_file(self.connection)
                rec["auth_error"] = {"at": _utcnow(), "status": resp.status_code, "body": resp.text[:300]}
                _write_token_file(rec, self.connection)
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
        resp = _request(
            "get", PRICE_HISTORY_URL,
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
        resp = _request(
            "get", QUOTES_URL,
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
        resp = _request("get", OPTION_CHAIN_URL, headers=self._auth_headers(), params=params, timeout=20)
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
        resp = _request(
            "get", INSTRUMENTS_URL, headers=self._auth_headers(),
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
        resp = _request("get", url, headers=self._auth_headers(), params=params, timeout=30)
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

    def cash_balance(self, force: bool = False, account_number: str | None = None) -> float:
        """Tradable cash of the account this book trades — the SAME account order
        placement uses, so the Level 5 dry-powder check can't read one account's
        cash while the order goes to another.

        Which account that is: ``account_number`` when given, else the active
        dashboard account's binding (accounts.py), else the first linked account
        (the historical single-account behaviour). The /accounts response is
        briefly cached whole, so per-account selection costs no extra round-trip.
        Raises SchwabError on failure — callers degrade to the stored manual value
        rather than block on this.
        """
        global _accounts_cache
        with _accounts_lock:
            now = time.time()
            if not force and _accounts_cache and now - _accounts_cache[0] < _ACCOUNTS_TTL:
                nodes = _accounts_cache[1]
            else:
                nodes = self.get_accounts(positions=False)
                _accounts_cache = (now, nodes)
        if not nodes:
            raise SchwabError("schwab: no linked accounts")
        cash = _account_cash(select_account_node(
            nodes, account_number if account_number is not None else bound_account_number()))
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

    # -- truthful submission (incident hotfix, D2) ---------------------------
    # HTTP status codes that are an EXPLICIT order rejection carrying a reason we
    # can trust and display. Auth/rate/5xx/network are NOT rejections — the order
    # may or may not have reached the order engine, so they resolve to UNKNOWN and
    # are re-queried, never shown as "failed". LIVE_VERIFY: confirm the exact set
    # of client-rejection codes against a live account.
    _EXPLICIT_REJECT_CODES = (400, 422)

    def submit_order(self, account_hash: str, order: dict) -> dict:
        """Place a REAL order and return a STRUCTURED outcome instead of raising —
        the ack handler needs to tell "Schwab rejected this" apart from "we never
        heard back". Never raises for an HTTP-level result; only a programming error
        would propagate. Outcomes:

          {"outcome": "accepted", "order_id": str|None, "location": str}
              HTTP 200/201 — the order is live. order_id is parsed from the Location
              header (None if the header is absent — still accepted; the caller marks
              it UNKNOWN and resolves by recent-orders match, never "failed").
          {"outcome": "rejected", "status_code": int, "reason": str}
              An EXPLICIT broker rejection (see _EXPLICIT_REJECT_CODES) with the
              body preserved verbatim as the reason.
          {"outcome": "unknown", "status_code": int|None, "detail": str}
              No response / timeout / auth / rate-limit / 5xx — ambiguous. The order
              may be live; the caller confirms with the broker before claiming
              anything. status_code is None when the request never got a response.
        """
        url = f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders"
        try:
            resp = requests.post(
                url, headers=self._auth_headers({"Content-Type": "application/json"}),
                json=order, timeout=30,
            )
        except requests.exceptions.RequestException as e:
            # Timeout / connection reset — the POST may have reached Schwab and the
            # order may be working. UNKNOWN, never failed (D2).
            return {"outcome": "unknown", "status_code": None,
                    "detail": f"no response from broker (request error): {e}"}
        if resp.status_code in (200, 201):
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            order_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
            return {"outcome": "accepted", "order_id": order_id, "location": location}
        body = (resp.text or "").strip()
        if resp.status_code in self._EXPLICIT_REJECT_CODES:
            return {"outcome": "rejected", "status_code": resp.status_code,
                    "reason": body or f"Schwab rejected the order (HTTP {resp.status_code})"}
        return {"outcome": "unknown", "status_code": resp.status_code,
                "detail": body or f"HTTP {resp.status_code}"}

    def get_order(self, account_hash: str, order_id: str) -> dict:
        return self._get_json(f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders/{order_id}") or {}

    def list_orders(self, account_hash: str, from_entered_time: str | None = None,
                    to_entered_time: str | None = None, max_results: int = 50) -> list[dict]:
        """Recent orders for an account, newest window first — used to RECOVER an
        orderId when a 2xx ack carried no Location header (D4). LIVE_VERIFY: the
        exact query-param names/format (fromEnteredTime/toEnteredTime, ISO-8601) and
        the response array shape are unconfirmed against a live account; the caller
        matches on leg symbols + time and treats a miss as still-UNKNOWN, so a wrong
        assumption here degrades safely (never a false match)."""
        params: dict = {"maxResults": max_results}
        if from_entered_time:
            params["fromEnteredTime"] = from_entered_time
        if to_entered_time:
            params["toEnteredTime"] = to_entered_time
        out = self._get_json(f"{ACCOUNTS_BASE}/accounts/{account_hash}/orders", params=params)
        return out if isinstance(out, list) else []

    def get_transactions(self, account_hash: str, start_date: str | None = None,
                         end_date: str | None = None,
                         types: str = "TRADE") -> list[dict]:
        """Settled transactions (executions) for an account over a date window —
        the GROUND-TRUTH feed for execution ingestion (INGESTION_IS_GROUND_TRUTH).
        Read-only; no CFM_LIVE_TRADING needed.

        LIVE_VERIFY: the exact endpoint path, query-param names/format
        (``startDate``/``endDate`` ISO-8601), the ``types`` filter value(s), and
        the transaction object shape (``activityId``, ``time``, ``type``,
        ``orderId``, ``transferItems[]`` with ``instrument``/``amount``/``cost``/
        ``price``/``positionEffect``/``feeType``) are assumptions stubbed behind
        this interface and MUST be confirmed against a captured live response
        before ingestion is trusted unsupervised. transaction_ingest.py parses
        defensively and drops anything it can't understand (loudly, into the
        report's ``errors``), so a wrong assumption here fails closed — an
        unparsed transaction is never silently ingested."""
        params: dict = {}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if types:
            params["types"] = types
        out = self._get_json(
            f"{ACCOUNTS_BASE}/accounts/{account_hash}/transactions", params=params or None)
        return out if isinstance(out, list) else []

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
def account_node_number(node: dict) -> str:
    """The plain account number on one /accounts response node."""
    return str(((node or {}).get("securitiesAccount") or {}).get("accountNumber") or "").strip()


def bound_account_number() -> str | None:
    """The brokerage account number the ACTIVE dashboard account trades, or None
    when it isn't bound to one (single-account installs, and any account the
    operator hasn't pointed at a specific Schwab account yet)."""
    try:
        import accounts as account_registry
        return account_registry.broker_account_number()
    except Exception as e:  # noqa: BLE001 — a registry problem must not break reads
        logger.warning("could not read the account binding: %s", e)
        return None


def select_account_node(nodes: list[dict], account_number: str | None) -> dict:
    """Pick the /accounts node for one brokerage account.

    ``account_number`` selects it; ``None`` reads the first linked account (the
    historical single-account behaviour). A bound number that isn't in the
    response RAISES rather than silently falling back: the whole point of binding
    is that this book's numbers come from that account and no other.
    """
    if not account_number:
        return nodes[0]
    for node in nodes:
        if account_node_number(node) == str(account_number).strip():
            return node
    raise SchwabError(
        f"schwab: account {str(account_number)[-4:]} is bound to this book but is not "
        "linked to this login — re-link it or clear the binding in Settings → Accounts")


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


def build_net_order(legs: list[tuple], net_price: float, *,
                    complex_strategy_type: str = "CUSTOM", duration: str = "DAY") -> dict:
    """A single multi-leg NET_CREDIT/NET_DEBIT order so several option legs fill
    together or not at all — no legging risk. ``legs`` is a list of (instruction,
    option_symbol, quantity). ``net_price`` is per share: positive = net credit
    received, negative = net debit paid. Used for atomic entries (buy-to-open the
    LEAP + sell-to-open the weekly short), atomic exits (sell-to-close the LEAP +
    buy-to-close the short), and atomic LEAP rolls.

    ``complex_strategy_type`` and ``duration`` are parameters (not hardcoded) so
    the ATOMIC ENTRY can route them through its provenance-tagged config constants
    (config.ENTRY_COMPLEX_STRATEGY_TYPE / ENTRY_ORDER_DURATION), matching how the
    roll already reads ROLL_COMPLEX_STRATEGY_TYPE / ROLL_ORDER_DURATION. The
    defaults preserve the exit / LEAP-roll behavior unchanged (CUSTOM / DAY)."""
    credit = float(net_price) >= 0
    return {
        "orderType": "NET_CREDIT" if credit else "NET_DEBIT",
        "session": "NORMAL",
        "price": f"{abs(float(net_price)):.2f}",
        "duration": duration,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": complex_strategy_type,
        "orderLegCollection": [
            {"instruction": instr, "quantity": int(qty),
             "instrument": {"symbol": sym, "assetType": "OPTION"}}
            for instr, sym, qty in legs
        ],
    }


def build_roll_order(quantity: int, buy_to_close_symbol: str, sell_to_open_symbol: str,
                     net_price, order_type: str | None = None) -> dict:
    """A single two-leg NET_CREDIT/NET_DEBIT DAY order for a short-call roll:
    buy-to-close the old short + sell-to-open the new one on ONE ticket, so the
    roll cannot leg out (fill one side, miss the other). `net_price` is per
    share: positive = credit received, negative = debit paid. It may be a Decimal
    (preferred — the executor builds a tick-rounded Decimal via order_pricing) or a
    float; the price is serialized EXACTLY (no binary-float artifact) either way.

    `order_type` is derived from the sign of `net_price` when omitted (back-compat
    for paper / direct callers). When the executor has already computed the
    direction in one place (order_pricing.net_credit_debit), it passes the derived
    NET_CREDIT/NET_DEBIT explicitly and this asserts the two agree — a contradiction
    between the computed direction and the constructed order is an error, never a
    silently-flipped submission (D1(b)).

    `duration` and `complexOrderStrategyType` come from config
    (ROLL_ORDER_DURATION / ROLL_COMPLEX_STRATEGY_TYPE). CUSTOM is the safe
    superset covering any strike/expiration combination (vertical or diagonal);
    the exact enum Schwab's spread approval wants is a LIVE_VERIFY item — see
    config.py — so it is a constant, not hardcoded here."""
    derived = order_pricing.NET_CREDIT if _dec_ge_zero(net_price) else order_pricing.NET_DEBIT
    if order_type is None:
        order_type = derived
    elif order_type != derived:
        raise AssertionError(
            f"build_roll_order: caller passed order_type={order_type} but net_price "
            f"{net_price} implies {derived} — refusing to build a contradictory order")
    return {
        "orderType": order_type,
        "session": "NORMAL",
        "price": order_pricing.format_price(net_price),
        "duration": config.ROLL_ORDER_DURATION,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": config.ROLL_COMPLEX_STRATEGY_TYPE,
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


def build_equity_order(instruction: str, quantity: int, symbol: str,
                       limit_price: float | None = None) -> dict:
    """A single-leg equity order for the SHARES base leg (schema v20).

    ``instruction`` is the equity verb ("BUY" / "SELL"); ``quantity`` is a SHARE
    count (NOT option contracts x100); ``symbol`` is the plain ticker. A LIMIT
    order when ``limit_price`` is given, else MARKET.

    LIVE_VERIFY — the app has NEVER transmitted an equity order. Every field here
    is the believed Schwab equity-order shape but is UNCONFIRMED against a live
    ``previewOrder``: the equity instruction verbs ("BUY"/"SELL" vs the option
    BUY_TO_OPEN family), ``assetType: "EQUITY"`` as an ORDER field (it appears only
    in READ parsing today), and the share-count quantity semantics. This builder is
    used ONLY to construct a previewOrder payload; NO equity order is transmitted by
    this migration. Capture an accepted previewOrder JSON and reconcile before any
    place_order path is enabled (see AUDIT_SHARES_PRIMARY_MIGRATION_PHASE0.md §7)."""
    order = {
        "orderType": "LIMIT" if limit_price is not None else "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity": int(quantity),
            "instrument": {"symbol": symbol, "assetType": "EQUITY"},  # LIVE_VERIFY
        }],
    }
    if limit_price is not None:
        order["price"] = f"{float(limit_price):.2f}"
    return order


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
    return _parse_chain(payload, "callExpDateMap")


def parse_put_chain(payload: dict) -> tuple[float | None, list[dict]]:
    """The same, for putExpDateMap — the cash-secured put's chain (schema v22).

    Schwab returns both sides in ONE payload, so this needs no extra fetch: the
    call chain is already pulled and cached per ticker, and the put side has been
    riding along unread except for `parse_put_iv` / `parse_put_quotes`, which
    mine it for skew-aware call vol. Same normalized shape as the call side, so
    `indicators.get_nearby_strikes` and the rest work on it unchanged.

    Note the sign convention Schwab uses: put deltas come back NEGATIVE. Callers
    that want "how far out of the money" should read the magnitude.
    """
    return _parse_chain(payload, "putExpDateMap")


def _parse_chain(payload: dict, map_key: str) -> tuple[float | None, list[dict]]:
    """Shared flattener for either side of the chain. One parser, so a field the
    call side normalizes can never quietly differ on the put side."""
    underlying = _num(payload.get("underlyingPrice"))
    if underlying is None:
        u = payload.get("underlying") or {}
        underlying = _num(u.get("last")) or _num(u.get("mark"))

    contracts: list[dict] = []
    for exp_key, strikes in (payload.get(map_key) or {}).items():
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
