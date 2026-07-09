"""License enforcement for cmd-council — Model C2 (all-in subscription).

In C2 YOU host one central gateway with YOUR Command Code key(s) and sell
a yearly subscription. Two things must be enforced on every request:

  1. VALIDITY  — signed token, not expired (the 1-year kill switch),
                 not revoked, bound to the right client.
  2. USAGE CAP — because YOU pay the model bill, each license has a
                 monthly credit ceiling so one client can't drain your
                 Command Code budget. Exceeding it returns HTTP 429.

Tokens are HMAC-SHA256 signed (stdlib only — no extra deps). Since the
issuer and verifier are both your own server, a shared secret is enough;
no asymmetric keys needed. Keep LICENSE_SECRET private and off client
machines.

Token wire format:  base64url(payload_json) + "." + base64url(hmac)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# HTTP-ish status codes used by the gateway to signal the client.
CODE_MISSING = 401       # no token
CODE_INVALID = 403       # bad signature / malformed / revoked
CODE_EXPIRED = 402       # subscription ended (the 1-year kill switch)
CODE_QUOTA = 429         # monthly usage cap hit


class LicenseError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------
# token sign / verify (HMAC-SHA256, stdlib)
# ----------------------------------------------------------------------

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(payload: dict[str, Any], secret: str) -> str:
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    """Verify signature and decode claims. Raises LicenseError on failure.

    Does NOT check expiry/revoke/quota — that's check_request()'s job.
    """
    try:
        body, sig = token.strip().split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        sig_ok = hmac.compare_digest(_b64u_dec(sig), expected)
    except (ValueError, Exception):  # malformed base64 / structure
        raise LicenseError(CODE_INVALID, "license key malformed")
    if not sig_ok:
        raise LicenseError(CODE_INVALID, "license signature invalid")
    try:
        return json.loads(_b64u_dec(body))
    except (ValueError, json.JSONDecodeError):
        raise LicenseError(CODE_INVALID, "license payload invalid")


# ----------------------------------------------------------------------
# license store (SQLite)
# ----------------------------------------------------------------------

@dataclass
class License:
    key_id: str
    client: str
    product: str
    issued_at: float
    expires_at: float
    status: str            # active | revoked
    bind: str              # e.g. "tg:948213771" (empty = unbound)
    monthly_quota_usd: float
    usage_usd: float
    period_start: float    # start of current monthly usage window


def _month_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()


class LicenseStore:
    def __init__(self, path: str | Path = "data/licenses.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS licenses(
                key_id TEXT PRIMARY KEY,
                client TEXT NOT NULL,
                product TEXT NOT NULL,
                issued_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bind TEXT NOT NULL DEFAULT '',
                monthly_quota_usd REAL NOT NULL DEFAULT 0,
                usage_usd REAL NOT NULL DEFAULT 0,
                period_start REAL NOT NULL DEFAULT 0
            )"""
        )
        # idempotency ledger for payment webhooks (one row per transaction)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS webhook_events(
                txn_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                key_id TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL DEFAULT 0
            )"""
        )
        self._db.commit()

    def issue(
        self,
        key_id: str,
        client: str,
        product: str,
        days: int = 365,
        monthly_quota_usd: float = 0.0,
        bind: str = "",
    ) -> License:
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO licenses VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key_id, client, product, now, now + days * 86400, "active",
             bind, monthly_quota_usd, 0.0, _month_start(now)),
        )
        self._db.commit()
        return self.get(key_id)

    def get(self, key_id: str) -> License | None:
        row = self._db.execute(
            "SELECT * FROM licenses WHERE key_id=?", (key_id,)
        ).fetchone()
        return License(**dict(row)) if row else None

    def renew(self, key_id: str, days: int = 365) -> License | None:
        lic = self.get(key_id)
        if not lic:
            return None
        # extend from whichever is later: now or current expiry (no lost days)
        base = max(time.time(), lic.expires_at)
        self._db.execute(
            "UPDATE licenses SET expires_at=?, status='active' WHERE key_id=?",
            (base + days * 86400, key_id),
        )
        self._db.commit()
        return self.get(key_id)

    def revoke(self, key_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE licenses SET status='revoked' WHERE key_id=?", (key_id,)
        )
        self._db.commit()
        return cur.rowcount > 0

    def record_usage(self, key_id: str, cost_usd: float) -> None:
        lic = self.get(key_id)
        if not lic:
            return
        now = time.time()
        # reset the monthly window if we've rolled into a new calendar month
        if _month_start(now) > lic.period_start:
            self._db.execute(
                "UPDATE licenses SET usage_usd=?, period_start=? WHERE key_id=?",
                (cost_usd, _month_start(now), key_id),
            )
        else:
            self._db.execute(
                "UPDATE licenses SET usage_usd=usage_usd+? WHERE key_id=?",
                (cost_usd, key_id),
            )
        self._db.commit()

    def list(self) -> list[License]:
        rows = self._db.execute(
            "SELECT * FROM licenses ORDER BY issued_at DESC"
        ).fetchall()
        return [License(**dict(r)) for r in rows]

    # ---- webhook helpers ------------------------------------------

    def mark_event(self, txn_id: str, provider: str = "",
                   action: str = "", key_id: str = "") -> bool:
        """Record a payment event once. Returns False if already seen
        (idempotency guard — payment providers retry webhooks)."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO webhook_events VALUES(?,?,?,?,?)",
            (txn_id, provider, action, key_id, time.time()),
        )
        self._db.commit()
        return cur.rowcount > 0

    def update_event(self, txn_id: str, action: str, key_id: str = "") -> None:
        self._db.execute(
            "UPDATE webhook_events SET action=?, key_id=? WHERE txn_id=?",
            (action, key_id, txn_id),
        )
        self._db.commit()

    def find_by_bind(self, bind: str, product: str | None = None) -> License | None:
        """Latest active license bound to a caller identity (e.g. tg:123).
        Used to resolve renewals that arrive without an explicit key id."""
        q = "SELECT * FROM licenses WHERE bind=? AND status='active'"
        args: list[Any] = [bind]
        if product:
            q += " AND product=?"
            args.append(product)
        q += " ORDER BY expires_at DESC LIMIT 1"
        row = self._db.execute(q, args).fetchone()
        return License(**dict(row)) if row else None


# ----------------------------------------------------------------------
# the check the gateway runs on every request
# ----------------------------------------------------------------------

@dataclass
class LicenseContext:
    key_id: str
    client: str
    product: str
    days_left: float
    quota_left_usd: float | None  # None = unlimited


def check_request(
    token: str,
    store: LicenseStore,
    secret: str,
    *,
    grace_days: int = 7,
    bind: str | None = None,
) -> LicenseContext:
    """Full gatekeeper: signature -> DB status -> expiry(+grace) -> quota.

    `bind` (optional) is the caller identity (e.g. "tg:948213771") to match
    against the license's binding, blocking key sharing across devices.
    Raises LicenseError (with .code) on any failure.
    """
    if not token:
        raise LicenseError(CODE_MISSING, "license key tidak ada")

    claims = verify_token(token, secret)  # raises on bad signature
    key_id = claims.get("lic")
    if not key_id:
        raise LicenseError(CODE_INVALID, "license key tanpa id")

    lic = store.get(key_id)
    if lic is None:
        raise LicenseError(CODE_INVALID, "lisensi tidak dikenal")
    if lic.status == "revoked":
        raise LicenseError(CODE_INVALID, "lisensi dicabut — hubungi admin")

    now = time.time()
    hard_stop = lic.expires_at + grace_days * 86400
    if now >= hard_stop:
        raise LicenseError(
            CODE_EXPIRED,
            "LISENSI KEDALUWARSA — masa langganan & tenggang berakhir. "
            "Silakan perpanjang.",
        )

    if bind and lic.bind and bind != lic.bind:
        raise LicenseError(
            CODE_INVALID, "lisensi terikat ke perangkat/akun lain"
        )

    # usage cap (0 quota = unlimited)
    quota_left: float | None = None
    if lic.monthly_quota_usd > 0:
        used = lic.usage_usd if _month_start(now) <= lic.period_start else 0.0
        quota_left = lic.monthly_quota_usd - used
        if quota_left <= 0:
            raise LicenseError(
                CODE_QUOTA,
                "Kuota pemakaian bulanan lisensi ini habis. "
                "Reset awal bulan depan, atau upgrade paket.",
            )

    return LicenseContext(
        key_id=key_id,
        client=lic.client,
        product=lic.product,
        days_left=max(0.0, (lic.expires_at - now) / 86400),
        quota_left_usd=quota_left,
    )
