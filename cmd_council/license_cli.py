"""License admin CLI (run on YOUR machine, never a client's).

    council-license keygen                       # buat LICENSE_SECRET baru
    council-license issue  --client cli_7h2x --product ukmppd \
                           --days 365 --quota 3.0 --bind tg:948213771
    council-license renew  --key UKMPPD-8F3A-... [--days 365]
    council-license revoke --key UKMPPD-8F3A-...
    council-license list
    council-license token  --key UKMPPD-8F3A-...  # cetak ulang token klien

Secret & DB dibaca dari env / argumen:
    LICENSE_SECRET   (wajib untuk issue/token/keygen-verify)
    LICENSE_DB       (default: data/licenses.db)
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from datetime import datetime, timezone

from .license import LicenseStore, sign_token


def _secret() -> str:
    s = os.environ.get("LICENSE_SECRET", "")
    if not s:
        print("error: set LICENSE_SECRET (jalankan: council-license keygen)",
              file=sys.stderr)
        sys.exit(1)
    return s


def _store() -> LicenseStore:
    return LicenseStore(os.environ.get("LICENSE_DB", "data/licenses.db"))


def _new_key_id(product: str) -> str:
    return f"{product.upper()}-{secrets.token_hex(2).upper()}-{secrets.token_hex(3).upper()}"


def _make_token(key_id: str, client: str, product: str, expires_at: float) -> str:
    return sign_token(
        {"lic": key_id, "client": client, "product": product,
         "iat": int(time.time()), "exp": int(expires_at)},
        _secret(),
    )


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="council-license")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="cetak LICENSE_SECRET acak (simpan aman!)")

    pi = sub.add_parser("issue", help="terbitkan lisensi baru")
    pi.add_argument("--client", required=True)
    pi.add_argument("--product", required=True)
    pi.add_argument("--days", type=int, default=365)
    pi.add_argument("--quota", type=float, default=0.0,
                    help="kuota model USD/bulan (0 = tak terbatas)")
    pi.add_argument("--bind", default="", help='mis. "tg:948213771"')

    pr = sub.add_parser("renew", help="perpanjang lisensi")
    pr.add_argument("--key", required=True)
    pr.add_argument("--days", type=int, default=365)

    pv = sub.add_parser("revoke", help="cabut lisensi sekarang")
    pv.add_argument("--key", required=True)

    sub.add_parser("list", help="daftar semua lisensi")

    pt = sub.add_parser("token", help="cetak ulang token untuk klien")
    pt.add_argument("--key", required=True)

    args = p.parse_args(argv)

    if args.cmd == "keygen":
        print("LICENSE_SECRET=" + secrets.token_urlsafe(48))
        print("# simpan di env server gateway; JANGAN pernah kirim ke klien.")
        return

    store = _store()

    if args.cmd == "issue":
        key_id = _new_key_id(args.product)
        lic = store.issue(key_id, args.client, args.product,
                          days=args.days, monthly_quota_usd=args.quota,
                          bind=args.bind)
        token = _make_token(key_id, lic.client, lic.product, lic.expires_at)
        print(f"✓ lisensi terbit: {key_id}")
        print(f"  klien   : {lic.client}  ·  produk: {lic.product}")
        print(f"  berlaku : {_fmt(lic.issued_at)} → {_fmt(lic.expires_at)} "
              f"({args.days} hari)")
        print(f"  kuota   : {'tak terbatas' if args.quota == 0 else f'${args.quota:.2f}/bln'}")
        if args.bind:
            print(f"  terikat : {args.bind}")
        print("\n  LICENSE KEY untuk klien (pasang di config hermes):\n")
        print("  " + token)

    elif args.cmd == "renew":
        lic = store.renew(args.key, days=args.days)
        if not lic:
            print("error: key tidak ditemukan", file=sys.stderr); sys.exit(1)
        token = _make_token(lic.key_id, lic.client, lic.product, lic.expires_at)
        print(f"✓ diperpanjang → {_fmt(lic.expires_at)}")
        print("  token baru:\n  " + token)

    elif args.cmd == "revoke":
        ok = store.revoke(args.key)
        print("✓ dicabut" if ok else "key tidak ditemukan")

    elif args.cmd == "list":
        rows = store.list()
        if not rows:
            print("(belum ada lisensi)"); return
        now = time.time()
        for l in rows:
            left = (l.expires_at - now) / 86400
            state = l.status if l.status == "revoked" else (
                "AKTIF" if left > 0 else "KEDALUWARSA")
            print(f"{l.key_id:28} {l.product:10} {state:12} "
                  f"exp {_fmt(l.expires_at)} ({left:+.0f}h)  "
                  f"pakai ${l.usage_usd:.2f}"
                  + (f"/${l.monthly_quota_usd:.0f}" if l.monthly_quota_usd else ""))

    elif args.cmd == "token":
        lic = store.get(args.key)
        if not lic:
            print("error: key tidak ditemukan", file=sys.stderr); sys.exit(1)
        print(_make_token(lic.key_id, lic.client, lic.product, lic.expires_at))


if __name__ == "__main__":
    main()
