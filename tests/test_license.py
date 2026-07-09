"""Tests for the license gateway (token sign/verify + expiry + quota)."""
import time

import pytest

from cmd_council.license import (
    CODE_EXPIRED,
    CODE_INVALID,
    CODE_MISSING,
    CODE_QUOTA,
    LicenseError,
    LicenseStore,
    check_request,
    sign_token,
    verify_token,
)

SECRET = "test-secret-xyz"


def store(tmp_path):
    return LicenseStore(tmp_path / "lic.db")


def token_for(key_id, exp, secret=SECRET, client="c1", product="ukmppd"):
    return sign_token(
        {"lic": key_id, "client": client, "product": product,
         "iat": int(time.time()), "exp": int(exp)},
        secret,
    )


def test_sign_verify_roundtrip():
    tok = sign_token({"lic": "K1", "exp": 999}, SECRET)
    claims = verify_token(tok, SECRET)
    assert claims["lic"] == "K1"


def test_verify_rejects_tampered_signature():
    tok = sign_token({"lic": "K1"}, SECRET)
    with pytest.raises(LicenseError) as e:
        verify_token(tok, "wrong-secret")
    assert e.value.code == CODE_INVALID


def test_missing_token(tmp_path):
    with pytest.raises(LicenseError) as e:
        check_request("", store(tmp_path), SECRET)
    assert e.value.code == CODE_MISSING


def test_valid_license_passes(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-1", "c1", "ukmppd", days=365)
    ctx = check_request(token_for("K-1", lic.expires_at), s, SECRET)
    assert ctx.client == "c1"
    assert 360 < ctx.days_left <= 365


def test_expired_beyond_grace_blocked(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-2", "c1", "ukmppd", days=365)
    # force expiry 10 days ago (past the 7-day grace)
    s._db.execute("UPDATE licenses SET expires_at=? WHERE key_id='K-2'",
                  (time.time() - 10 * 86400,))
    s._db.commit()
    with pytest.raises(LicenseError) as e:
        check_request(token_for("K-2", time.time() - 10 * 86400), s, SECRET)
    assert e.value.code == CODE_EXPIRED


def test_within_grace_still_works(tmp_path):
    s = store(tmp_path)
    s.issue("K-3", "c1", "ukmppd", days=365)
    s._db.execute("UPDATE licenses SET expires_at=? WHERE key_id='K-3'",
                  (time.time() - 3 * 86400,))  # expired 3d ago, grace=7d
    s._db.commit()
    ctx = check_request(token_for("K-3", time.time() - 3 * 86400), s, SECRET)
    assert ctx.days_left == 0.0  # clamped, but not blocked


def test_revoked_blocked(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-4", "c1", "ukmppd", days=365)
    s.revoke("K-4")
    with pytest.raises(LicenseError) as e:
        check_request(token_for("K-4", lic.expires_at), s, SECRET)
    assert e.value.code == CODE_INVALID


def test_binding_mismatch_blocked(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-5", "c1", "ukmppd", days=365, bind="tg:111")
    with pytest.raises(LicenseError) as e:
        check_request(token_for("K-5", lic.expires_at), s, SECRET, bind="tg:999")
    assert e.value.code == CODE_INVALID
    # correct binding passes
    assert check_request(token_for("K-5", lic.expires_at), s, SECRET, bind="tg:111")


def test_quota_exhausted_blocks(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-6", "c1", "ukmppd", days=365, monthly_quota_usd=1.0)
    s.record_usage("K-6", 1.2)  # over the $1 cap
    with pytest.raises(LicenseError) as e:
        check_request(token_for("K-6", lic.expires_at), s, SECRET)
    assert e.value.code == CODE_QUOTA


def test_quota_left_reported(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-7", "c1", "ukmppd", days=365, monthly_quota_usd=5.0)
    s.record_usage("K-7", 2.0)
    ctx = check_request(token_for("K-7", lic.expires_at), s, SECRET)
    assert abs(ctx.quota_left_usd - 3.0) < 1e-9


def test_renew_extends(tmp_path):
    s = store(tmp_path)
    lic = s.issue("K-8", "c1", "ukmppd", days=365)
    before = lic.expires_at
    renewed = s.renew("K-8", days=365)
    assert renewed.expires_at > before + 300 * 86400
