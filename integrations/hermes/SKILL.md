---
name: council
description: "Tanya panel model (LLM Council) lalu sintesis jawaban final oleh chairman."
version: 1.0.0
metadata:
  hermes:
    tags: [council, multi-model, second-opinion]
---

# Council

Skill ini menghubungkan Hermes Agent ke layanan **cmd-council** (LLM Council
di atas Command Code): beberapa advisor model menjawab paralel, saling
me-review secara anonim, lalu satu chairman menyintesis jawaban final.

Saat dipanggil sebagai `/council <pertanyaan>` (opsional `--mode eco|standard|max`):

1. Panggil tool `run_council` dengan argumen:
   - `question`: pertanyaan user apa adanya (tanpa flag mode).
   - `mode`: `"eco"`, `"standard"`, atau `"max"` jika user menyebutnya;
     jika tidak, jangan kirim mode (biarkan default layanan).
2. Tampilkan hasil tool APA ADANYA (sudah berformat markdown):
   Jawaban Final di atas, lalu ringkasan panel per advisor, tabel ranking
   agregat, dan catatan konsensus/kontradiksi/blind spot.
3. JANGAN menambahkan opinimu sendiri ke Jawaban Final dan JANGAN menjawab
   pertanyaannya sendiri seolah-olah hasil council.
4. Jika tool gagal karena layanan tidak berjalan, beri tahu user cara
   menjalankannya:
   `cd cmd-council && council serve --port 8400`
   (atau set env `COUNCIL_URL` jika layanan ada di host/port lain).
5. Jika tool gagal karena budget guard (HTTP 429), sampaikan bahwa kuota
   rolling window Command Code hampir habis dan sarankan `--mode eco`
   atau menunggu window reset.
