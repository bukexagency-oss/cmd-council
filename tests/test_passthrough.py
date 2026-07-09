"""Tests for P0 fixes: passthrough facade, session purge, per-product quota."""
import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from cmd_council.license import LicenseStore, sign_token
from cmd_council.models import TokenUsage
from cmd_council.provider import CommandCodeProvider
from cmd_council.server import create_app
from cmd_council.storage import SessionStore


# ---------------------------------------------------------------- purge

def test_purge_removes_only_old_sessions(tmp_path):
    store = SessionStore(tmp_path)
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    assert store.purge_older_than(30) == 1
    assert not old.exists() and new.exists()


def test_purge_disabled_with_zero_days(tmp_path):
    store = SessionStore(tmp_path)
    f = tmp_path / "x.json"
    f.write_text("{}")
    stale = time.time() - 400 * 86400
    os.utime(f, (stale, stale))
    assert store.purge_older_than(0) == 0
    assert f.exists()


# ------------------------------------------------------- passthrough facade

@pytest.fixture()
def pt_client(tmp_path, monkeypatch):
    asyncio.set_event_loop(asyncio.new_event_loop())
    monkeypatch.setenv("CMD_API_KEY", "dummy")
    monkeypatch.setenv("LICENSE_SECRET", "sek")
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "l.db"))
    monkeypatch.delenv("WEBHOOK_SHARED_SECRET", raising=False)

    async def fake_chat_raw(self, ref, *, messages, max_tokens, temperature=0.7):
        assert messages[0]["role"] == "system"      # riwayat penuh diteruskan
        return "halo dari passthrough", TokenUsage(1000, 200)

    monkeypatch.setattr(CommandCodeProvider, "chat_raw", fake_chat_raw)
    app = create_app("council.yaml")
    store = LicenseStore(tmp_path / "l.db")
    store.issue("K-PT", "c1", "ppds", days=365, monthly_quota_usd=3.0)
    token = sign_token({"lic": "K-PT"}, "sek")
    return TestClient(app), store, token


def test_passthrough_single_model_and_metering(pt_client):
    c, store, token = pt_client
    r = c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "chat-eco", "messages": [
            {"role": "system", "content": "kamu adalah Vignette"},
            {"role": "user", "content": "halo"},
        ]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "halo dari passthrough"
    assert body["usage"]["total_tokens"] == 1200
    lic = store.get("K-PT")
    assert lic.usage_usd > 0          # biaya tercatat ke lisensi (metering C2)
    assert lic.usage_usd < 0.001      # …dan SANGAT murah (bukan sesi council)


def test_passthrough_requires_license(pt_client):
    c, _, _ = pt_client
    r = c.post("/v1/chat/completions",
               json={"model": "chat-eco",
                     "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 401


def test_passthrough_listed_in_health_and_errors(pt_client):
    c, _, token = pt_client
    h = c.get("/health").json()
    assert set(h["passthrough_modes"]) == {"chat", "chat-eco"}
    assert h["session_retention_days"] == 30
    r = c.post("/v1/chat/completions",
               headers={"Authorization": f"Bearer {token}"},
               json={"model": "tidak-ada", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 404 and "chat-eco" in r.json()["detail"]


# ------------------------------------------------- per-product quota (webhook)

def test_webhook_quota_map_per_product(tmp_path, monkeypatch):
    asyncio.set_event_loop(asyncio.new_event_loop())
    monkeypatch.setenv("CMD_API_KEY", "dummy")
    monkeypatch.setenv("LICENSE_SECRET", "sek")
    monkeypatch.setenv("LICENSE_DB", str(tmp_path / "l.db"))
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "hooky")
    monkeypatch.setenv("WEBHOOK_QUOTA_MAP", "ukmppd:1.5, facharzt:5")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    app = create_app("council.yaml")
    c = TestClient(app)
    store = LicenseStore(tmp_path / "l.db")

    pay = {"event": "payment.received",
           "data": {"id": "q-1", "status": "SUCCESS", "customerName": "Budi",
                    "productName": "Paket UKMPPD",
                    "customField": [{"name": "telegram_id", "value": "111"}]}}
    key = c.post("/webhooks/payment?secret=hooky", json=pay).json()["key_id"]
    assert store.get(key).monthly_quota_usd == 1.5   # dari peta, bukan default

    pay2 = {"event": "payment.received",
            "data": {"id": "q-2", "status": "SUCCESS", "customerName": "Rani",
                     "productName": "Paket Pflege Jerman",
                     "customField": [{"name": "telegram_id", "value": "222"}]}}
    key2 = c.post("/webhooks/payment?secret=hooky", json=pay2).json()["key_id"]
    assert store.get(key2).monthly_quota_usd == 3.0  # fallback default
