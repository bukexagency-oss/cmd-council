"""Tests for the payment webhook (issue/renew + idempotency)."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from cmd_council.license import LicenseStore
from cmd_council.server import create_app
from cmd_council.webhook import parse_payment

SECRET = "hook-secret-abc"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Earlier test modules use asyncio.run(), which leaves the main-thread
    # event loop unset on Python 3.9 — create_app needs one (Semaphore).
    asyncio.set_event_loop(asyncio.new_event_loop())
    monkeypatch.setenv("CMD_API_KEY", "dummy")
    monkeypatch.setenv("LICENSE_SECRET", "lic-secret-xyz")
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "lic.db"))
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", SECRET)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)  # delivery: skipped
    app = create_app("council.yaml")
    return TestClient(app), LicenseStore(tmp_path / "lic.db")


MAYAR = {
    "event": "payment.received",
    "data": {
        "id": "txn-001",
        "status": "SUCCESS",
        "customerName": "Budi Dokter",
        "customerEmail": "budi@contoh.id",
        "productName": "Paket UKMPPD Lisensi Tahunan",
        "amount": 990000,
        "customField": [{"name": "telegram_id", "value": "948213771"}],
    },
}

LYNK = {
    "event": "payment_received",
    "order_id": "LNK-77",
    "status": "PAID",
    "buyer_name": "Sari Perawat",
    "email": "sari@contoh.id",
    "item_name": "Paket Pflege Jerman",
    "total": 1490000,
    "telegram_id": "555111",
}


def test_parse_mayar_shape():
    ev = parse_payment(MAYAR)
    assert ev.ok and ev.product == "ukmppd" and ev.telegram_id == "948213771"
    assert ev.txn_id == "txn-001" and not ev.is_renewal


def test_parse_lynk_shape():
    ev = parse_payment(LYNK)
    assert ev.ok and ev.product == "pflege" and ev.telegram_id == "555111"
    assert ev.txn_id == "LNK-77"


def test_wrong_secret_rejected(client):
    c, _ = client
    r = c.post("/webhooks/payment?secret=salah", json=MAYAR)
    assert r.status_code == 401


def test_issue_on_success_payment(client):
    c, store = client
    r = c.post(f"/webhooks/payment?secret={SECRET}", json=MAYAR)
    body = r.json()
    assert r.status_code == 200 and body["action"] == "issued"
    lic = store.get(body["key_id"])
    assert lic.product == "ukmppd" and lic.bind == "tg:948213771"
    assert lic.monthly_quota_usd == 3.0
    assert lic.expires_at - time.time() > 360 * 86400
    assert body["telegram"] == "skipped"  # no bot token in tests


def test_duplicate_txn_ignored(client):
    c, store = client
    c.post(f"/webhooks/payment?secret={SECRET}", json=MAYAR)
    r2 = c.post(f"/webhooks/payment?secret={SECRET}", json=MAYAR)
    assert r2.json()["action"] == "duplicate"
    assert len(store.list()) == 1


def test_non_success_ignored(client):
    c, store = client
    pending = {"event": "payment.created",
               "data": {**MAYAR["data"], "id": "txn-x", "status": "PENDING"}}
    r = c.post(f"/webhooks/payment?secret={SECRET}", json=pending)
    assert r.json()["action"] == "ignored"
    assert store.list() == []


def test_webinar_purchase_skipped(client):
    c, store = client
    tix = {"event": "payment.received",
           "data": {"id": "txn-w", "status": "SUCCESS",
                    "customerName": "Andi", "productName": "Tiket Webinar AI Agent",
                    "amount": 149000}}
    r = c.post(f"/webhooks/payment?secret={SECRET}", json=tix)
    assert r.json()["action"] == "skipped"
    assert store.list() == []


def test_renewal_extends_same_key(client):
    c, store = client
    first = c.post(f"/webhooks/payment?secret={SECRET}", json=MAYAR).json()
    exp1 = first["expires_at"]
    renew = {"event": "payment.received",
             "data": {"id": "txn-002", "status": "SUCCESS",
                      "customerName": "Budi Dokter",
                      "productName": "Perpanjangan Paket UKMPPD",
                      "amount": 990000,
                      "customField": [{"name": "telegram_id", "value": "948213771"}]}}
    r = c.post(f"/webhooks/mayar?secret={SECRET}", json=renew).json()
    assert r["action"] == "renewed"
    assert r["key_id"] == first["key_id"]          # key TIDAK berganti
    assert r["expires_at"] > exp1 + 300 * 86400     # +1 tahun dari exp lama
    assert len(store.list()) == 1                   # tidak ada lisensi kedua


def test_renewal_without_match_flags_admin(client):
    c, store = client
    orphan = {"event": "payment.received",
              "data": {"id": "txn-orphan", "status": "SUCCESS",
                       "customerName": "Tanpa Jejak",
                       "productName": "Perpanjangan Paket PPDS",
                       "customField": [{"name": "telegram_id", "value": "000999"}]}}
    r = c.post(f"/webhooks/payment?secret={SECRET}", json=orphan).json()
    assert r["action"] == "renewal_unmatched"
    assert store.list() == []
