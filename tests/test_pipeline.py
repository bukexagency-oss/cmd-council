"""End-to-end pipeline tests with a fake provider (no network)."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from cmd_council.budget import BudgetGuard, Ledger
from cmd_council.config import (
    AppConfig,
    ModeConfig,
    ModelRef,
    PriceEntry,
    ProviderConfig,
)
from cmd_council.models import TokenUsage
from cmd_council.pipeline import (
    BudgetExceededError,
    CouncilPipeline,
    QuorumError,
)
from cmd_council.pricing import PriceBook
from cmd_council.provider import ProviderError
from cmd_council.storage import SessionStore

LABEL_RE = re.compile(r"### (Response [A-Z])")


class FakeProvider:
    """Duck-typed CommandCodeProvider: canned answers, optional failures."""

    def __init__(self, fail_models: set[str] | None = None,
                 unparseable_reviewers: set[str] | None = None) -> None:
        self.fail_models = fail_models or set()
        self.unparseable_reviewers = unparseable_reviewers or set()
        self.calls: list[tuple[str, str]] = []  # (model, kind)

    @staticmethod
    def _kind(user: str) -> str:
        # Check chairman first: its prompt embeds review excerpts, which
        # themselves contain the "FINAL RANKING" marker.
        if "final synthesis" in user:
            return "chairman"
        if "FINAL RANKING" in user:
            return "review"
        return "advise"

    async def chat(self, ref, *, system, user, max_tokens, temperature=0.7):
        kind = self._kind(user)
        self.calls.append((ref.model, kind))
        if ref.model in self.fail_models:
            raise ProviderError(f"{ref.model}: HTTP 500: boom", 500)

        if kind == "advise":
            return (
                f"Answer from {ref.model}: use approach X because of Y.",
                TokenUsage(100, 60),
            )
        if kind == "review":
            labels = LABEL_RE.findall(user)
            if ref.model in self.unparseable_reviewers:
                return ("I refuse to produce the requested block.", TokenUsage(300, 40))
            ordered = sorted(labels)  # deterministic ranking
            block = "\n".join(f"{i + 1}. {lab}" for i, lab in enumerate(ordered))
            return (
                f"Critique of all responses...\n\nFINAL RANKING:\n{block}\n",
                TokenUsage(400, 80),
            )
        return (
            "Final Answer\nUse approach X.\n\nConsensus\n- X is right.\n\n"
            "Contradictions\n- none major.\n\nBlind spots\n- none.",
            TokenUsage(700, 120),
        )

    async def stream_chat(self, ref, *, system, user, max_tokens, temperature=0.7):
        text, usage = await self.chat(
            ref, system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        for i in range(0, len(text), 20):
            yield ("token", text[i : i + 20])
        yield ("usage", usage)


def make_cfg() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(api_key="test"),
        default_mode="standard",
        modes={
            "standard": ModeConfig(
                advisors=[
                    ModelRef(model="m1"),
                    ModelRef(model="m2"),
                    ModelRef(model="m3", protocol="anthropic"),
                ],
                chairman=ModelRef(model="chair", protocol="anthropic"),
                review=True,
            ),
            "eco": ModeConfig(
                advisors=[ModelRef(model="m1"), ModelRef(model="m2")],
                chairman=ModelRef(model="cheap-chair"),
                review=False,
            ),
        },
        pricing_fallback={
            "m1": PriceEntry(input=0.6, output=2.2),
            "m2": PriceEntry(input=0.6, output=2.2),
            "m3": PriceEntry(input=0.6, output=2.2),
            "chair": PriceEntry(input=2.0, output=10.0),
            "cheap-chair": PriceEntry(input=1.0, output=5.0),
        },
    )


def make_pipeline(tmp_path, provider: FakeProvider, cfg: AppConfig | None = None):
    cfg = cfg or make_cfg()
    pb = PriceBook(
        cfg.pricing_fallback, cfg.credit_multipliers, cache_path=tmp_path / "m.json"
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    guard = BudgetGuard(cfg, ledger, pb)
    storage = SessionStore(tmp_path / "conv")
    return CouncilPipeline(cfg, provider, guard, pb, storage), ledger, storage


def test_full_run(tmp_path):
    provider = FakeProvider()
    pipeline, ledger, storage = make_pipeline(tmp_path, provider)
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    result = asyncio.run(
        pipeline.run("Redis or Postgres for a small job queue?",
                     emit=emit, stream_final=False)
    )

    assert result.final.startswith("Final Answer")
    assert result.chairman_model == "chair"
    assert len(result.stage1) == 3 and all(a.ok for a in result.stage1)
    assert len(result.label_map) == 3
    assert len(result.aggregate_ranking) == 3
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0
    assert result.cost_est > 0
    assert not result.degraded

    # persistence + ledger: 3 stage1 + 3 stage2 + 1 stage3 = 7 entries
    assert storage.get(result.id) is not None
    lines = [json.loads(l) for l in ledger.path.read_text().splitlines() if l.strip()]
    assert len(lines) == 7

    types = [e["type"] for e in events]
    for expected in (
        "stage1_start", "stage1_complete", "stage2_start", "stage2_complete",
        "stage3_start", "stage3_complete", "usage", "complete",
    ):
        assert expected in types


def test_graceful_degradation(tmp_path):
    provider = FakeProvider(fail_models={"m2"})
    pipeline, _ledger, _storage = make_pipeline(tmp_path, provider)
    result = asyncio.run(pipeline.run("q?", stream_final=False))
    assert result.degraded == ["m2"]
    assert len(result.label_map) == 2
    assert len(result.aggregate_ranking) == 2
    assert result.final


def test_quorum_error(tmp_path):
    provider = FakeProvider(fail_models={"m2", "m3"})
    pipeline, _ledger, _storage = make_pipeline(tmp_path, provider)
    with pytest.raises(QuorumError):
        asyncio.run(pipeline.run("q?", stream_final=False))


def test_eco_skips_review(tmp_path):
    provider = FakeProvider()
    pipeline, ledger, _storage = make_pipeline(tmp_path, provider)
    result = asyncio.run(pipeline.run("q?", mode="eco", stream_final=False))
    assert result.mode == "eco"
    assert result.reviews == []
    assert result.aggregate_ranking == []
    assert result.final
    lines = ledger.path.read_text().splitlines()
    assert len([l for l in lines if l.strip()]) == 3  # 2 stage1 + 1 stage3


def test_stream_final_tokens(tmp_path):
    provider = FakeProvider()
    pipeline, _ledger, _storage = make_pipeline(tmp_path, provider)
    tokens: list[str] = []

    async def emit(ev):
        if ev["type"] == "stage3_token":
            tokens.append(ev["token"])

    result = asyncio.run(pipeline.run("q?", emit=emit, stream_final=True))
    assert tokens and "".join(tokens) == result.final


def test_unparseable_review_without_fallback(tmp_path):
    provider = FakeProvider(unparseable_reviewers={"m1"})
    pipeline, _ledger, _storage = make_pipeline(tmp_path, provider)
    result = asyncio.run(pipeline.run("q?", stream_final=False))
    # m1's vote is dropped; aggregation still works from m2 + m3.
    bad = [rv for rv in result.reviews if rv.reviewer_model == "m1"][0]
    assert bad.ranking is None
    assert len(result.aggregate_ranking) == 3
    assert all(item.votes == 2 for item in result.aggregate_ranking)


def test_budget_hard_stop(tmp_path):
    provider = FakeProvider()
    pipeline, ledger, _storage = make_pipeline(tmp_path, provider)
    import time as _time
    entry = {
        "ts": _time.time(), "model": "x", "stage": "stage1",
        "input_tokens": 1, "output_tokens": 1, "cost": 8.9, "session_id": "s",
    }
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    with pytest.raises(BudgetExceededError):
        asyncio.run(pipeline.run("q?", stream_final=False))
