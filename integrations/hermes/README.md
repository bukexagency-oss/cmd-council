# Integrasi Hermes Agent — `/council`

Dua file di folder ini menghubungkan [hermes-agent](https://github.com/nousresearch/hermes-agent)
ke layanan cmd-council.

## Prasyarat

Layanan cmd-council berjalan (default port 8400):

```bash
cd cmd-council
export CMD_API_KEY=...       # API key Command Code
council serve --port 8400
```

## Instalasi

1. **Skill** — hermes otomatis mengubah skill menjadi slash command
   (`agent/skill_commands.py`). Salin folder skill ke direktori skill
   user-local:

   ```bash
   mkdir -p ~/.hermes/skills/council
   cp SKILL.md ~/.hermes/skills/council/SKILL.md
   ```

   (Alternatif in-repo: `skills/misc/council/SKILL.md` di checkout hermes-agent.)

2. **Tool** — salin `council_tool.py` ke direktori `tools/` di checkout
   hermes-agent, lalu daftarkan sesuai konvensi tool di sana (modul
   Python biasa dengan skema function-calling; lihat `TOOL_SCHEMA` di
   file ini). Tool dieksekusi thread-pool oleh `agent/tool_executor.py`,
   jadi HTTP blocking aman. Set timeout tool ≥ 180 detik — sesi council
   penuh memakan 40–120 detik.

3. (Opsional) Jika layanan council tidak di localhost:8400:

   ```bash
   export COUNCIL_URL=http://host-lain:8400
   ```

## Pemakaian

```
/council Apakah arsitektur X lebih baik daripada Y untuk kasus Z?
/council --mode eco Pertanyaan ringan yang tidak butuh review silang
/council --mode max Keputusan penting yang layak panel premium
```

## Jalur alternatif — council sebagai "model"

hermes-agent mendukung provider OpenAI-compatible kustom
(`plugins/model-providers/custom/`). Arahkan base URL provider kustom ke
facade cmd-council (`http://localhost:8400/v1`) lalu pilih model
`council` / `council-eco` / `council-max` dengan `/model`. Dengan jalur
ini SETIAP giliran chat menjadi sesi council (biaya per giliran naik
±9×) — cocok untuk sesi khusus, bukan default harian.
