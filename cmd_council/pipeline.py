"""Three-stage council orchestration.

karpathy/llm-council flow (parallel first opinions -> anonymized peer
review + ranking -> chairman synthesis) hardened with openfusion-style
patterns: graceful degradation with a quorum, per-call ledger recording,
budget pre-flight with auto-downgrade, and stage-3 token streaming.
"""
from __future__ import annotations

import asyncio
import random
import string
from typing import Awaitable, Callable

from . import prompts
from . import ranking as ranking_mod
from .budget import BudgetGuard
from .config import AppConfig, ModelRef
from .models import AdvisorAnswer, AggregateRankItem, CouncilResult, Review, TokenUsage
from .pricing import PriceBook
from .provider import CommandCodeProvider, ProviderError
from .storage import SessionStore

EmitFn = Callable[[dict], Awaitable[None]]

REVIEW_CLIP_CHARS = 700  # excerpt length of each review passed to the chairman


class CouncilError(Exception):
    pass


class QuorumError(CouncilError):
    pass


class BudgetExceededError(CouncilError):
    pass


async def _noop_emit(event: dict) -> None:  # pragma: no cover - trivial
    return None


class CouncilPipeline:
    def __init__(
        self,
        cfg: AppConfig,
        provider: CommandCodeProvider,
        guard: BudgetGuard,
        price_book: PriceBook,
        storage: SessionStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.guard = guard
        self.price_book = price_book
        self.storage = storage

    # ------------------------------------------------------------------
    # public entrypoint
    # ------------------------------------------------------------------

    async def run(
        self,
        question: str,
        mode: str | None = None,
        *,
        force: bool = False,
        emit: EmitFn | None = None,
        stream_final: bool = True,
        review_override: bool | None = None,
    ) -> CouncilResult:
        emit = emit or _noop_emit
        question = (question or "").strip()
        if not question:
            raise CouncilError("question is empty")

        requested = mode or self.cfg.default_mode
        if requested not in self.cfg.modes:
            raise CouncilError(
                f"unknown mode {requested!r}; available: {', '.join(self.cfg.modes)}"
            )

        decision = self.guard.preflight(requested, force=force)
        if not decision.allowed:
            raise BudgetExceededError(decision.reason)

        mode_name = decision.mode
        mode_cfg = self.cfg.modes[mode_name]
        do_review = mode_cfg.review if review_override is None else review_override

        result = CouncilResult(question=question, mode=mode_name)
        if decision.downgraded:
            result.downgraded_from = requested
        result.warnings.extend(decision.warnings)

        # ---------------- Stage 1: first opinions (parallel) ----------------
        await emit(
            {
                "type": "stage1_start",
                "mode": mode_name,
                "models": [a.model for a in mode_cfg.advisors],
            }
        )
        answers = await asyncio.gather(
            *[self._advise(ref, question, result.id) for ref in mode_cfg.advisors]
        )
        pairs = list(zip(mode_cfg.advisors, answers))
        ok_pairs = [(ref, ans) for ref, ans in pairs if ans.ok]
        result.stage1 = list(answers)
        result.degraded = [ans.model for _, ans in pairs if not ans.ok]
        for ans in answers:
            result.usage.add(ans.usage)
            result.cost_est += ans.cost_est

        quorum = max(1, self.cfg.limits.quorum)
        if len(ok_pairs) < quorum:
            detail = "; ".join(
                f"{ans.model}: {ans.error or 'empty'}" for _, ans in pairs if not ans.ok
            )
            raise QuorumError(
                f"quorum not met ({len(ok_pairs)}/{quorum} advisors succeeded). {detail}"
            )

        # Anonymize: shuffle order, assign labels; the label->model map
        # stays server-side (models never see who wrote what).
        shuffled = [ans for _, ans in ok_pairs]
        random.shuffle(shuffled)
        for i, ans in enumerate(shuffled):
            ans.label = f"Response {string.ascii_uppercase[i]}"
        result.label_map = {a.label: a.model for a in shuffled if a.label}
        labeled = [(a.label, a.text) for a in shuffled if a.label]
        labels = [a.label for a in shuffled if a.label]

        await emit(
            {
                "type": "stage1_complete",
                "results": [
                    {
                        "model": a.model,
                        "label": a.label,
                        "answer": a.text,
                        "error": a.error,
                    }
                    for a in answers
                ],
            }
        )

        # ---------------- Stage 2: anonymized peer review ----------------
        agg_lines: list[str] = []
        if do_review and len(ok_pairs) >= 2:
            await emit(
                {"type": "stage2_start", "models": [ref.model for ref, _ in ok_pairs]}
            )
            reviews = await asyncio.gather(
                *[
                    self._review(ref, question, labeled, labels, result.id)
                    for ref, _ in ok_pairs
                ]
            )
            fallback_ref = self.cfg.utility.ranking_fallback_parser
            for rv in reviews:
                if rv.error is None and rv.ranking is None and fallback_ref is not None:
                    rv.ranking = await self._fallback_parse(rv.text, labels, result.id)
                result.usage.add(rv.usage)
                result.cost_est += rv.cost_est
            result.reviews = list(reviews)

            valid = {rv.reviewer_model: rv.ranking for rv in reviews if rv.ranking}
            agg_dicts = ranking_mod.aggregate_rankings(valid, result.label_map)
            result.aggregate_ranking = [AggregateRankItem(**d) for d in agg_dicts]
            agg_lines = [
                f"{i + 1}. {d['label']} — avg rank {d['avg_rank']} ({d['votes']} votes)"
                for i, d in enumerate(agg_dicts)
            ]
            await emit(
                {
                    "type": "stage2_complete",
                    "aggregate_ranking": agg_dicts,
                    "reviews": [
                        {
                            "reviewer": rv.reviewer_model,
                            "ranking": rv.ranking,
                            "text": rv.text,
                            "error": rv.error,
                        }
                        for rv in reviews
                    ],
                }
            )

        # ---------------- Stage 3: chairman synthesis ----------------
        chairman = mode_cfg.chairman
        await emit({"type": "stage3_start", "model": chairman.model})

        review_digest = [
            (f"reviewer {i + 1}", rv.text[:REVIEW_CLIP_CHARS])
            for i, rv in enumerate(result.reviews)
            if rv.text
        ] or None
        user_prompt = prompts.stage3_user(question, labeled, agg_lines, review_digest)
        max_out = self.cfg.limits.max_output_tokens.stage3

        final = ""
        usage = TokenUsage()
        if stream_final:
            try:
                chunks: list[str] = []
                async for kind, payload in self.provider.stream_chat(
                    chairman,
                    system=prompts.STAGE3_SYSTEM,
                    user=user_prompt,
                    max_tokens=max_out,
                ):
                    if kind == "token":
                        chunks.append(payload)
                        await emit({"type": "stage3_token", "token": payload})
                    elif kind == "usage":
                        usage = payload
                final = "".join(chunks)
            except ProviderError as e:
                await emit(
                    {
                        "type": "stage3_fallback",
                        "message": f"stream failed ({e}); retrying without streaming",
                    }
                )
                final, usage = "", TokenUsage()

        if not final.strip():
            final, usage = await self.provider.chat(
                chairman,
                system=prompts.STAGE3_SYSTEM,
                user=user_prompt,
                max_tokens=max_out,
            )
        if not final.strip():
            raise CouncilError(f"chairman {chairman.model} returned an empty synthesis")

        cost = self._record(chairman.model, "stage3", usage, result.id)
        result.usage.add(usage)
        result.cost_est += cost
        result.final = final
        result.chairman_model = chairman.model

        await emit({"type": "stage3_complete", "final": final, "model": chairman.model})
        await emit(
            {
                "type": "usage",
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_est": round(result.cost_est, 6),
            }
        )

        if self.storage is not None:
            try:
                self.storage.save(result)
            except OSError as e:  # never fail a session over persistence
                result.warnings.append(f"failed to persist session: {e}")

        await emit({"type": "complete", "id": result.id, "mode": result.mode})
        return result

    # ------------------------------------------------------------------
    # per-call helpers
    # ------------------------------------------------------------------

    async def _advise(
        self, ref: ModelRef, question: str, session_id: str
    ) -> AdvisorAnswer:
        try:
            text, usage = await self.provider.chat(
                ref,
                system=prompts.STAGE1_SYSTEM,
                user=question,
                max_tokens=self.cfg.limits.max_output_tokens.stage1,
            )
        except ProviderError as e:
            return AdvisorAnswer(model=ref.model, error=str(e))
        ans = AdvisorAnswer(model=ref.model, text=text or "", usage=usage)
        ans.cost_est = self._record(ref.model, "stage1", usage, session_id)
        if not ans.text.strip():
            ans.error = "empty response"
        return ans

    async def _review(
        self,
        ref: ModelRef,
        question: str,
        labeled: list[tuple[str, str]],
        labels: list[str],
        session_id: str,
    ) -> Review:
        try:
            text, usage = await self.provider.chat(
                ref,
                system=None,
                user=prompts.stage2_user(question, labeled),
                max_tokens=self.cfg.limits.max_output_tokens.stage2,
            )
        except ProviderError as e:
            return Review(reviewer_model=ref.model, error=str(e))
        rv = Review(reviewer_model=ref.model, text=text or "", usage=usage)
        rv.cost_est = self._record(ref.model, "stage2", usage, session_id)
        rv.ranking = ranking_mod.parse_ranking(rv.text, labels)
        return rv

    async def _fallback_parse(
        self, review_text: str, labels: list[str], session_id: str
    ) -> list[str] | None:
        """Rescue an unparseable FINAL RANKING with a cheap utility model.

        Cost is recorded in the ledger (stage='utility') but is negligible
        (~1e-4 $) and intentionally not added to the session total.
        """
        ref = self.cfg.utility.ranking_fallback_parser
        if ref is None:
            return None
        try:
            text, usage = await self.provider.chat(
                ref,
                system=None,
                user=prompts.ranking_extraction_user(review_text, labels),
                max_tokens=120,
                temperature=0.0,
            )
        except ProviderError:
            return None
        self._record(ref.model, "utility", usage, session_id)
        return ranking_mod.parse_ranking_json(text, labels)

    def _record(
        self, model: str, stage: str, usage: TokenUsage, session_id: str
    ) -> float:
        cost, _known = self.price_book.estimate(
            model, usage.input_tokens, usage.output_tokens
        )
        try:
            self.guard.ledger.record(
                model=model,
                stage=stage,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=cost,
                session_id=session_id,
            )
        except OSError:
            pass
        return cost
