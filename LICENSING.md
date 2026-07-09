# Lisensi Tahunan (Model C2) — cmd-council License Gateway

Enforcement lisensi 1 tahun untuk semua vertical (UKMPPD, PPDS, Facharzt,
Pflege, Hebamme, bisnis). **Kamu** menjalankan satu gateway pusat dengan
API key Command Code milikmu; klien bayar langganan tahunan. Saat lewat 1
tahun (+ tenggang), seluruh agent klien mati serentak.

## Prinsip

Enforcement hidup di **servermu** (choke point), bukan di mesin klien —
karena software di mesin klien selalu bisa dibongkar. Setiap agent hermes
menarik napas (panggilan model) lewat gateway ini, jadi satu tanggal
`expires_at` mengendalikan seluruh armada klien.

## Aktivasi

Gateway = cmd-council + set env `LICENSE_SECRET`. Tanpa env itu, licensing
mati (mode self-host single-tenant — berguna untuk dev).

```bash
# 1. buat secret (SEKALI, simpan aman di server; JANGAN kirim ke klien)
council-license keygen
#   -> LICENSE_SECRET=xxxxxxxx

# 2. jalankan gateway dengan lisensi + kuota aktif
export LICENSE_SECRET=xxxxxxxx
export LICENSE_DB=data/licenses.db      # default
export LICENSE_GRACE_DAYS=7             # tenggang setelah exp (default 7)
export CMD_API_KEY=...                  # API key Command Code MILIKMU (C2)
council serve --host 0.0.0.0 --port 8400
```

## Menjual & mengelola lisensi

```bash
# terbitkan lisensi 1 tahun, kuota model $3/bln, terikat Telegram klien
council-license issue --client cli_budi --product ukmppd \
    --days 365 --quota 3.0 --bind tg:948213771
#   -> mencetak LICENSE KEY (token) untuk dipasang di config hermes klien

council-license renew  --key UKMPPD-CA14-D4FFE1        # perpanjang 1 tahun
council-license revoke --key UKMPPD-CA14-D4FFE1        # matikan seketika (refund/abuse)
council-license list                                   # status + pemakaian semua lisensi
council-license token  --key UKMPPD-CA14-D4FFE1        # cetak ulang token klien
```

## Otomasi pembayaran → lisensi (webhook)

Bayar di Mayar/Lynk.id → lisensi terbit/perpanjang otomatis → key
meluncur ke Telegram klien. Aktifkan dengan env tambahan:

```bash
export WEBHOOK_SHARED_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
export TELEGRAM_BOT_TOKEN=...        # bot pengirim key (opsional)
export TELEGRAM_ADMIN_CHAT_ID=...    # chat kamu, untuk notifikasi (opsional)
# opsional: WEBHOOK_DEFAULT_DAYS=365  WEBHOOK_DEFAULT_QUOTA=3.0
```

Daftarkan URL ini di dashboard webhook provider pembayaranmu:

```
https://gateway.[domain]/webhooks/payment?secret=<WEBHOOK_SHARED_SECRET>
```

(secret juga diterima via header `x-webhook-secret` / `x-callback-token` /
`Authorization: Bearer`.) Alias tersedia: `/webhooks/mayar`, `/webhooks/lynkid`.

Perilaku:
- **Parser toleran** — membaca bentuk Mayar (camelCase + `customField`),
  Lynk.id, dan payload generik. Telegram ID diambil dari custom field
  checkout bernama `telegram_id` (pasang field itu di halaman pembayaranmu).
- **Idempoten** — provider me-retry webhook; txn yang sama diproses sekali.
- **Perpanjangan** — nama produk mengandung "perpanjangan/renewal" →
  `renew` lisensi aktif dengan binding Telegram yang sama. **Klien tidak
  perlu mengubah apa pun**: token hanya membawa key id, masa aktif
  ditegakkan dari DB.
- **Tidak pernah gagal diam-diam** — pembayaran tak terpetakan (tanpa
  telegram_id, perpanjangan tanpa lisensi cocok) tetap dijawab 200 tapi
  admin di-DM untuk proses manual. Tiket webinar dikenali dan dilewati.
- **Uji sebelum live**: lakukan 1 transaksi tes dari dashboard provider dan
  cocokkan nama field payload — parser luwes, tapi verifikasi tetap wajib.

## Sisi klien (dipasang saat DFY)

Hanya dua baris berubah di config hermes klien — arahkan otak ke gateway,
sertakan license key:

```
base_url = https://gateway.[agensimu].com/v1
Authorization: Bearer <LICENSE_KEY>
x-client-bind: tg:948213771        # opsional, cegah key dibagi-bagi
```

SKILL.md, system prompt, dan struktur tim TIDAK berubah.

## Apa yang dicek pada tiap request

| Kondisi | Respons | Arti buat klien |
|---|---|---|
| Tak ada token | 401 | belum dikonfigurasi |
| Token palsu/rusak/dicabut | 403 | key tidak sah |
| Lewat exp + grace | **402** | **langganan berakhir → perpanjang** |
| Kuota model bulanan habis | 429 | pakai melebihi paket; reset awal bulan |
| Valid | 200 | jalan; sisa hari & kuota disertakan di respons |

Semua penolakan terjadi **sebelum** panggilan model — jadi klien
kedaluwarsa/over-quota tidak membebani tagihan Command Code-mu.

## Kuota per-lisensi (penting untuk C2)

Karena kamu yang bayar model, tiap lisensi punya `monthly_quota_usd`.
Gateway mencatat estimasi biaya tiap sesi (`record_usage`) dan menolak
(429) saat plafon bulanan tercapai; reset otomatis awal bulan kalender.
Set `--quota 0` untuk tak terbatas (tidak disarankan di C2).

## UX kedaluwarsa

- Respons sukses menyertakan `license.days_left` → tampilkan banner di
  hermes saat < 30 hari.
- Grace `LICENSE_GRACE_DAYS` (default 7): setelah exp, tetap jalan singkat
  agar klien sempat bayar; setelah itu 402 total.
- Perpanjangan = `council-license renew` → hidup lagi seketika (data klien
  tak pernah dihapus).

## Catatan C2

⚠️ Merutekan banyak klien lewat satu key Command Code = pola **reseller**.
Amankan izin resale / plan Enterprise dari Command Code sebelum skala
(lihat dok. "Desain Sistem Lisensi 1 Tahun" §6). Uptime gateway jadi
tanggung jawabmu — kalau gateway mati, semua klien mati.

✅ **Jalur tanpa izin: OpenRouter.** ToS OpenRouter (6 Jul 2026) mengizinkan
eksplisit menyematkan layanan mereka ke produk berbayar ("your customers…
incorporate the Service into your own products and services"). Pakai
`council.openrouter.yaml` (ID model + harga terverifikasi dari GET /models):

```bash
export OPENROUTER_API_KEY=...
council serve --config council.openrouter.yaml
```

Estimasi biaya (harga live 8 Jul 2026): standard $0.055/sesi · eco $0.021 ·
max $0.093. Kewajibanmu satu: pastikan klien patuh Model Terms tiap model
(masukkan klausul di ToS produkmu). Rekomendasi: OpenRouter untuk klien C2,
Command Code Pro untuk pemakaian internalmu sendiri.
