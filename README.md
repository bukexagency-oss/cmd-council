# cmd-council 🏛️

**LLM Council on a single [Command Code](https://commandcode.ai) subscription.**

Ask once — a panel of cross-lab **advisor** models answers in parallel,
anonymously peer-reviews and ranks each other, then a premium **chairman**
synthesizes the final answer. Inspired by
[karpathy/llm-council](https://github.com/karpathy/llm-council), hardened
with patterns from [openfusion](https://github.com/shahar-dagan/openfusion),
and tuned for the economics of Command Code's **Pro $15/mo** plan
($30 model credits, rolling $9/5h and $18/7d windows).

```
Stage 1  first opinions   — advisors answer in parallel
Stage 2  peer review      — anonymized ("Response A/B/C"), ranked, aggregated
Stage 3  synthesis        — chairman writes the final answer (+ consensus,
                            contradictions, blind spots)
```

**Economics** (default `standard` mode): 4 open-source flagship advisors
(DeepSeek, GLM, Kimi, Qwen — diversity per credit) + Claude Sonnet 5 as
chairman (premium only where it has the most leverage). ≈ $0.05/session
→ ~600 sessions/month inside the $30 quota. A budget guard tracks a local
ledger against the rolling windows and auto-downgrades
`max → standard → eco` before you hit the provider's caps.

## Quickstart

```bash
pip install -e .
export CMD_API_KEY=...        # from Command Code Studio

council validate              # Phase 0: config + live catalog checks
council models                # live catalog — align IDs in council.yaml!
council ask "Redis vs Postgres untuk job queue kecil?"
council ask --mode eco "pertanyaan ringan"     # skips peer review (N+1 calls)
council usage                 # window spend vs caps
council serve --port 8400     # REST API + OpenAI-compatible facade
```

> ⚠️ **Model IDs in `council.yaml` are placeholders** (from Command Code's
> public catalog, July 2026). Run `council models` and align them with the
> live catalog before real use. Answers follow the language of the question.

## REST API (port 8400)

| Endpoint | Description |
|---|---|
| `POST /api/council` | `{question, mode?, force?, skip_review?}` → full session JSON |
| `POST /api/council/stream` | SSE: `stage1_*`, `stage2_*`, `stage3_token`, `usage`, `complete` |
| `GET /api/models` | live catalog (updates the internal price book) |
| `GET /api/usage` | local ledger windows vs caps + per-mode estimates |
| `GET /api/sessions` | recent sessions (JSON files in `data/conversations/`) |
| `POST /v1/chat/completions` | **OpenAI-compatible facade** — see below |
| `GET /health` | liveness |

### Use it from any OpenAI client

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8400/v1", api_key="unused")
resp = client.chat.completions.create(
    model="council",              # or council-eco / council-max
    messages=[{"role": "user", "content": "Apakah X lebih baik dari Y?"}],
)
print(resp.choices[0].message.content)   # chairman's synthesis
# panel answers + ranking: resp.model_extra["council_metadata"]
```

Streaming works too (`stream=True`): stage-3 tokens stream as normal chunks;
stage-1/2 progress arrives as SSE comments (ignored by OpenAI SDKs).

## Hermes Agent integration (`/council`)

See [`integrations/hermes/`](integrations/hermes/README.md):
drop `SKILL.md` into `~/.hermes/skills/council/` and `council_tool.py`
into hermes' `tools/` — hermes auto-exposes the skill as the `/council`
slash command. The tool calls this service over HTTP (blocking is safe;
hermes runs tools in a thread pool).

## Configuration

Everything lives in [`council.yaml`](council.yaml): provider base URL +
`${CMD_API_KEY}`, three modes (advisors + chairman + review flag), token
ceilings per stage, timeout/retry/quorum/concurrency, budget windows +
soft/hard stops, a static price fallback, and revocable credit
multipliers ("Deals"). The live `GET /models` catalog overrides fallback
prices automatically whenever it's fetched.

## Design notes

- **Anonymization is server-side**: models never see whose answer is
  whose (labels are shuffled; the chairman also judges blind). Names are
  restored only in the presentation layer.
- **Graceful degradation**: failed advisors are dropped (1 retry with
  backoff for 429/5xx); the session proceeds with a quorum of ≥2.
- **Unparseable rankings** are rescued by a cheap utility model
  (`gemini-3.1-flash-lite`); if that fails too, that reviewer's vote is
  simply ignored — never the whole session.
- **The ledger is an estimate.** Command Code's own usage accounting is
  authoritative; Deals are promotional and revocable (ToS guarantees at
  most 1:1), so reconcile monthly.

## Known limitations (MVP)

- Each question is a fresh, standalone council (no multi-turn context).
- No per-request advisor/chairman override yet (edit `council.yaml`).
- Single-user, localhost-first: no auth on the API.
- No web UI — by design (CLI + API + hermes are the interfaces).

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

MIT license. Built from a PRD; see the project docs for the full spec.
