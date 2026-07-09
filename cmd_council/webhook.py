"""Payment webhook → auto issue/renew license + kirim key via Telegram.

Alur (Model C2):
    Mayar / Lynk.id ──POST──▶ /webhooks/payment?secret=...
        1. verifikasi shared secret (query/header)
        2. parse payload toleran (Mayar camelCase, Lynk.id, bentuk generik)
        3. idempotency guard per txn id (provider ME-RETRY webhook!)
        4. produk "perpanjangan..." → renew; selain itu → issue baru
        5. DM license key ke Telegram klien + notifikasi admin

Env:
    WEBHOOK_SHARED_SECRET    mengaktifkan endpoint (wajib)
    TELEGRAM_BOT_TOKEN       bot pengirim key (opsional → delivery skipped)
    TELEGRAM_ADMIN_CHAT_ID   chat admin untuk notifikasi (opsional)
    WEBHOOK_DEFAULT_DAYS     default 365
    WEBHOOK_DEFAULT_QUOTA    kuota model USD/bln, default 3.0

Catatan desain penting:
- **Renewal tanpa sentuh klien**: token lisensi hanya membawa key id;
  masa aktif ditegakkan dari DB (check_request), jadi renew = geser
  `expires_at` — token yang sudah terpasang tetap sah.
- **Tidak pernah gagal diam-diam**: payload yang tak dikenali / tanpa
  Telegram ID tetap dijawab 200 (agar provider berhenti retry) tapi
  SELALU dilaporkan ke admin untuk tindak lanjut manual.
- Pembelian webinar (bukan lisensi) dikenali dan dilewati.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request

from .license import LicenseStore, sign_token

# ----------------------------------------------------------------------
# payload parsing (toleran terhadap variasi antar-provider)
# ----------------------------------------------------------------------

_SUCCESS_TOKENS = {
    "success", "paid", "settlement", "completed", "berhasil",
    "payment.received", "payment_received", "payment.success",
}
_STATUS_KEYS = ("status", "payment_status", "transaction_status", "state")
_NAME_KEYS = ("customername", "customer_name", "buyer_name", "name", "fullname")
_EMAIL_KEYS = ("customeremail", "customer_email", "buyer_email", "email")
_PRODUCT_KEYS = ("productname", "product_name", "item_name", "product",
                 "producttitle", "item", "title")
_AMOUNT_KEYS = ("amount", "total", "gross_amount", "price", "nominal")
_TXN_KEYS = ("transactionid", "transaction_id", "order_id", "invoice_id",
             "merchantorderid", "payment_id", "id")
_TG_KEYS = ("telegram_id", "telegramid", "telegram", "tg_id", "tgid", "id_telegram")

_PRODUCTS = ("ukmppd", "ppds", "facharzt", "pflege", "hebamme", "bisnis")
_RENEW_WORDS = ("perpanjang", "renewal", "renew", "extend")
_SKIP_WORDS = ("webinar", "tiket")


def _walk(obj, prefix=""):
    """Yield (lowercased_key, value) untuk semua leaf di payload."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, str(k).lower())
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, prefix)
    else:
        yield prefix, obj


def _find(payload: dict, keys: tuple[str, ...]) -> str:
    flat = list(_walk(payload))
    for key in keys:                      # urutan keys = prioritas
        for k, v in flat:
            if k == key and v not in (None, ""):
                return str(v)
    return ""


def _find_telegram(payload: dict) -> str:
    """Telegram ID dari field langsung ATAU array custom field
    (Mayar: customField: [{name/label: 'telegram_id', value: ...}])."""
    direct = _find(payload, _TG_KEYS)
    if direct:
        return re.sub(r"\D", "", direct)  # buang '@'/spasi; sisakan digit
    def scan(obj):
        if isinstance(obj, dict):
            label = str(obj.get("name") or obj.get("label") or obj.get("key") or "").lower()
            if any(t in label for t in ("telegram", "tg_id")):
                val = obj.get("value") or obj.get("answer") or ""
                if val:
                    return re.sub(r"\D", "", str(val))
            for v in obj.values():
                r = scan(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = scan(item)
                if r:
                    return r
        return ""
    return scan(payload)


@dataclass
class PaymentEvent:
    ok: bool                # status pembayaran sukses?
    txn_id: str
    name: str
    email: str
    product_raw: str
    product: str            # kode produk ter-map
    is_renewal: bool
    is_skip: bool           # webinar/tiket — bukan lisensi
    telegram_id: str
    amount: str


def parse_payment(payload: dict) -> PaymentEvent:
    status = _find(payload, _STATUS_KEYS).lower()
    event = str(payload.get("event", "")).lower()
    ok = (status in _SUCCESS_TOKENS) or (
        not status and any(t in event for t in _SUCCESS_TOKENS)
    )
    product_raw = _find(payload, _PRODUCT_KEYS)
    praw = product_raw.lower()
    product = next((p for p in _PRODUCTS if p in praw), "bisnis")
    txn = _find(payload, _TXN_KEYS) or hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:24]
    return PaymentEvent(
        ok=ok,
        txn_id=txn,
        name=_find(payload, _NAME_KEYS),
        email=_find(payload, _EMAIL_KEYS),
        product_raw=product_raw,
        product=product,
        is_renewal=any(w in praw for w in _RENEW_WORDS),
        is_skip=any(w in praw for w in _SKIP_WORDS),
        telegram_id=_find_telegram(payload),
        amount=_find(payload, _AMOUNT_KEYS),
    )


# ----------------------------------------------------------------------
# telegram delivery (best-effort; tidak pernah menggagalkan webhook)
# ----------------------------------------------------------------------

async def _tg_send(chat_id: str, text: str) -> str:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot or not chat_id:
        return "skipped"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
            )
            return "sent" if r.status_code == 200 else f"failed:{r.status_code}"
    except Exception as e:  # jaringan dsb. — jangan gagalkan webhook
        return f"failed:{type(e).__name__}"


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y")


def _client_msg_new(name: str, product: str, expires_at: float, token: str) -> str:
    return (
        f"🎉 Pembayaran diterima — terima kasih, {name or 'kak'}!\n\n"
        f"Paket: {product.upper()} · lisensi aktif sampai {_fmt_date(expires_at)}\n\n"
        f"LICENSE KEY kamu (simpan baik-baik, jangan dibagikan):\n\n{token}\n\n"
        "Langkah berikutnya:\n"
        "• Tim kami memasang sistemmu ≤ 48 jam setelah form intake lengkap "
        "(cek pesan/email berisi link form)\n"
        "• Key ini nanti kami pasang di sistemmu — kamu tidak perlu "
        "melakukan apa-apa dengannya\n\n"
        "Butuh bantuan? Balas pesan ini."
    )


def _client_msg_renew(key_id: str, expires_at: float) -> str:
    return (
        f"✅ Perpanjangan berhasil! Lisensi {key_id} kini aktif sampai "
        f"{_fmt_date(expires_at)}.\n\n"
        "Tidak ada yang perlu kamu ubah — sistemmu langsung lanjut tanpa "
        "jeda. Terima kasih sudah setahun lagi bersama kami 🚀"
    )


# ----------------------------------------------------------------------
# router
# ----------------------------------------------------------------------

def _check_secret(request: Request, secret: str) -> bool:
    supplied = (
        request.query_params.get("secret", "")
        or request.headers.get("x-webhook-secret", "")
        or request.headers.get("x-callback-token", "")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    return bool(supplied) and hmac.compare_digest(supplied, secret)


def build_webhook_router(store: LicenseStore, license_secret: str) -> APIRouter:
    router = APIRouter()
    wh_secret = os.environ["WEBHOOK_SHARED_SECRET"]
    default_days = int(os.environ.get("WEBHOOK_DEFAULT_DAYS", "365"))
    default_quota = float(os.environ.get("WEBHOOK_DEFAULT_QUOTA", "3.0"))
    admin_chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")

    # Kuota model per produk — harga beda, kuota beda.
    # Format env: WEBHOOK_QUOTA_MAP="ukmppd:2,ppds:3,facharzt:5,pflege:3,hebamme:3,bisnis:3"
    quota_map: dict[str, float] = {}
    for part in os.environ.get("WEBHOOK_QUOTA_MAP", "").split(","):
        if ":" in part:
            k, _, v = part.partition(":")
            try:
                quota_map[k.strip().lower()] = float(v)
            except ValueError:
                pass

    def _token_for(key_id: str, client: str, product: str, exp: float) -> str:
        return sign_token(
            {"lic": key_id, "client": client, "product": product,
             "iat": int(time.time()), "exp": int(exp)},
            license_secret,
        )

    @router.post("/webhooks/payment")
    @router.post("/webhooks/mayar")
    @router.post("/webhooks/lynkid")
    async def payment_webhook(request: Request):
        if not _check_secret(request, wh_secret):
            raise HTTPException(status_code=401, detail="webhook secret salah")

        try:
            payload = await request.json()
        except json.JSONDecodeError:
            form = await request.form()          # sebagian provider kirim form
            payload = dict(form)
        provider = request.url.path.rsplit("/", 1)[-1]

        ev = parse_payment(payload)

        # 1) bukan pembayaran sukses → jawab 200 agar provider berhenti retry
        if not ev.ok:
            return {"ok": True, "action": "ignored", "reason": "status bukan sukses"}
        # 2) produk non-lisensi (tiket webinar dsb.)
        if ev.is_skip:
            await _tg_send(admin_chat,
                           f"ℹ️ Pembayaran non-lisensi: {ev.product_raw} — "
                           f"{ev.name} ({ev.amount}). Tidak ada lisensi diterbitkan.")
            return {"ok": True, "action": "skipped", "reason": "produk non-lisensi"}
        # 3) idempotency — txn yang sama hanya diproses sekali
        if not store.mark_event(ev.txn_id, provider=provider, action="processing"):
            return {"ok": True, "action": "duplicate", "txn_id": ev.txn_id}

        bind = f"tg:{ev.telegram_id}" if ev.telegram_id else ""

        # ---- RENEWAL ---------------------------------------------------
        if ev.is_renewal:
            lic = store.find_by_bind(bind, ev.product) if bind else None
            if lic is None and bind:
                lic = store.find_by_bind(bind)       # produk apa pun
            if lic is None:
                store.update_event(ev.txn_id, "renewal_unmatched")
                await _tg_send(admin_chat,
                               f"⚠️ PERPANJANGAN TAK TERPETAKAN: {ev.name} "
                               f"({ev.product_raw}, tg:{ev.telegram_id or '?'}, "
                               f"txn {ev.txn_id}). Proses manual: council-license renew.")
                return {"ok": True, "action": "renewal_unmatched",
                        "note": "admin dinotifikasi untuk proses manual"}
            renewed = store.renew(lic.key_id, days=default_days)
            store.update_event(ev.txn_id, "renewed", renewed.key_id)
            delivery = await _tg_send(ev.telegram_id,
                                      _client_msg_renew(renewed.key_id, renewed.expires_at))
            await _tg_send(admin_chat,
                           f"🔁 RENEWED {renewed.key_id} ({renewed.product}) — {ev.name}, "
                           f"exp baru {_fmt_date(renewed.expires_at)}. DM klien: {delivery}")
            return {"ok": True, "action": "renewed", "key_id": renewed.key_id,
                    "expires_at": renewed.expires_at, "telegram": delivery}

        # ---- ISSUE BARU ------------------------------------------------
        quota = quota_map.get(ev.product, default_quota)
        slug = re.sub(r"[^a-z0-9]+", "", (ev.email.split("@")[0] or ev.name or "klien").lower())[:16]
        key_id = f"{ev.product.upper()}-{_secrets.token_hex(2).upper()}-{_secrets.token_hex(3).upper()}"
        lic = store.issue(key_id, f"cli_{slug or 'anon'}", ev.product,
                          days=default_days, monthly_quota_usd=quota,
                          bind=bind)
        token = _token_for(key_id, lic.client, lic.product, lic.expires_at)
        store.update_event(ev.txn_id, "issued", key_id)

        delivery = await _tg_send(ev.telegram_id,
                                  _client_msg_new(ev.name, ev.product, lic.expires_at, token))
        if delivery != "sent":
            await _tg_send(admin_chat,
                           f"⚠️ KEY BELUM TERKIRIM ({delivery}) — {key_id} untuk {ev.name} "
                           f"(tg:{ev.telegram_id or 'TIDAK ADA'}). Kirim manual: "
                           f"council-license token --key {key_id}")
        await _tg_send(admin_chat,
                       f"🆕 ISSUED {key_id} ({ev.product}, {default_days}h, "
                       f"${quota}/bln) — {ev.name} <{ev.email}> "
                       f"Rp{ev.amount} txn {ev.txn_id}. DM klien: {delivery}")
        return {"ok": True, "action": "issued", "key_id": key_id,
                "expires_at": lic.expires_at, "telegram": delivery}

    return router
