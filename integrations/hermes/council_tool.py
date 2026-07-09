"""Hermes Agent tool: run_council.

Drop this file into hermes-agent's `tools/` directory (see README.md in
this folder). It calls a locally running cmd-council service. Blocking
HTTP is safe here: hermes executes tools in a thread pool
(agent/tool_executor.py), and a full council session takes 40-120s, so
the timeout is generous.

Env:
    COUNCIL_URL   base URL of the cmd-council service
                  (default: http://localhost:8400)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

COUNCIL_URL = os.environ.get("COUNCIL_URL", "http://localhost:8400")
TIMEOUT_SECONDS = 300

# OpenAI-style function schema (adapt to hermes' tool registration
# conventions if the local checkout differs).
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_council",
        "description": (
            "Ask the LLM Council: several advisor models answer in "
            "parallel, anonymously peer-review each other, and a chairman "
            "synthesizes the final answer. Use for questions where a "
            "second opinion or a high-stakes decision matters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The user's question, verbatim.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["eco", "standard", "max"],
                    "description": (
                        "Council mode: eco (cheap, no peer review), "
                        "standard (default), max (premium panel)."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}


def run_council(question: str, mode: str | None = None) -> str:
    """Run one council session and return a markdown report."""
    payload: dict = {"question": question}
    if mode:
        payload["mode"] = mode

    req = urllib.request.Request(
        f"{COUNCIL_URL.rstrip('/')}/api/council",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code == 429:
            return (
                "⚠️ Council ditolak oleh budget guard (kuota rolling window "
                f"hampir habis).\n\nDetail: {detail}\n\n"
                "Saran: coba `--mode eco`, tunggu window reset, atau "
                "jalankan ulang dengan force dari CLI."
            )
        return f"❌ Council error HTTP {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError) as e:
        return (
            f"❌ Tidak bisa menghubungi layanan council di {COUNCIL_URL} ({e}).\n"
            "Jalankan dulu: `council serve --port 8400` di folder cmd-council, "
            "atau set env COUNCIL_URL."
        )

    return format_council_markdown(data)


def format_council_markdown(data: dict) -> str:
    lines: list[str] = []
    mode = data.get("mode", "?")
    if data.get("downgraded_from"):
        mode = f"{mode} (diturunkan dari {data['downgraded_from']} oleh budget guard)"

    lines.append(f"## 🏛️ Council — Jawaban Final\n")
    lines.append(data.get("final", "(kosong)"))

    ranking = (data.get("stage2") or {}).get("aggregate_ranking") or []
    if ranking:
        lines.append("\n### 📊 Ranking panel (review silang anonim)")
        lines.append("| # | Model | Avg rank | Votes |")
        lines.append("|---|-------|----------|-------|")
        for i, item in enumerate(ranking, 1):
            lines.append(
                f"| {i} | {item.get('model')} | {item.get('avg_rank')} "
                f"| {item.get('votes')} |"
            )

    stage1 = data.get("stage1") or []
    ok_models = [a["model"] for a in stage1 if not a.get("error")]
    if ok_models:
        lines.append(
            f"\n### 🧑‍⚖️ Panel: {', '.join(ok_models)} · "
            f"Chairman: {data.get('chairman', '?')} · Mode: {mode}"
        )

    degraded = data.get("degraded") or []
    if degraded:
        lines.append(f"\n⚠️ Advisor gagal (dilewati): {', '.join(degraded)}")
    for w in data.get("warnings") or []:
        lines.append(f"\n⚠️ {w}")

    usage = data.get("usage") or {}
    if usage:
        lines.append(
            f"\n— {usage.get('input_tokens', 0)} in / "
            f"{usage.get('output_tokens', 0)} out tokens "
            f"≈ ${usage.get('cost_est', 0):.4f} kredit "
            f"· session `{data.get('id', '?')}`"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "Redis vs Postgres untuk job queue kecil?"
    print(run_council(q))
