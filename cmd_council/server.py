"""FastAPI application: council API, SSE streaming, OpenAI-compatible facade.

Endpoints (PRD §10):
  POST /api/council          run a full session (sync JSON)
  POST /api/council/stream   SSE: stage events + stage-3 tokens
  GET  /api/models           live catalog (proxy of provider GET /models)
  GET  /api/usage            local ledger windows vs caps
  POST /v1/chat/completions  OpenAI-compatible facade (model: council*)
  GET  /health               liveness
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .budget import BudgetGuard, Ledger
from .config import load_config
from .license import LicenseContext, LicenseError, LicenseStore, check_request
from .pipeline import (
    BudgetExceededError,
    CouncilError,
    CouncilPipeline,
    QuorumError,
)
from .pricing import PriceBook
from .provider import CommandCodeProvider, ProviderError
from .storage import SessionStore

# Facade model names -> mode (None = config default_mode).
FACADE_MODES: dict[str, str | None] = {
    "council": None,
    "council-eco": "eco",
    "council-standard": "standard",
    "council-max": "max",
}


class CouncilRequest(BaseModel):
    question: str
    mode: str | None = None
    force: bool = False
    skip_review: bool | None = None  # true = skip stage 2, false = force it


def _map_error(e: Exception) -> HTTPException:
    if isinstance(e, BudgetExceededError):
        return HTTPException(status_code=429, detail=str(e))
    if isinstance(e, QuorumError):
        return HTTPException(status_code=502, detail=str(e))
    if isinstance(e, (CouncilError, ProviderError)):
        return HTTPException(status_code=500, detail=str(e))
    return HTTPException(status_code=500, detail=f"internal error: {e}")


def _review_override(skip_review: bool | None) -> bool | None:
    return None if skip_review is None else (not skip_review)


def _last_user_text(messages: list) -> str | None:
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            ]
            joined = "\n".join(x for x in parts if x).strip()
            if joined:
                return joined
    return None


def create_app(config_path: str = "council.yaml") -> FastAPI:
    cfg, missing_env = load_config(config_path)
    price_book = PriceBook(cfg.pricing_fallback, cfg.credit_multipliers)
    ledger = Ledger(cfg.storage.ledger)
    guard = BudgetGuard(cfg, ledger, price_book)
    provider = CommandCodeProvider(cfg.provider, cfg.limits)
    storage = SessionStore(cfg.storage.data_dir)
    pipeline = CouncilPipeline(cfg, provider, guard, price_book, storage)

    app = FastAPI(title="cmd-council", version="0.1.0")
    app.add_event_handler("shutdown", provider.aclose)

    # ---- Session retention (ToS/PDP: log isi maks. 30 hari) --------
    retention_days = int(os.environ.get("SESSION_RETENTION_DAYS", "30"))

    async def _purge_loop() -> None:
        while True:
            try:
                storage.purge_older_than(retention_days)
            except Exception:  # pragma: no cover - housekeeping must not crash
                pass
            await asyncio.sleep(86400)

    def _start_purge() -> None:
        if retention_days > 0:
            app.state.purge_task = asyncio.get_event_loop().create_task(_purge_loop())

    def _stop_purge() -> None:
        task = getattr(app.state, "purge_task", None)
        if task and not task.done():
            task.cancel()

    app.add_event_handler("startup", _start_purge)
    app.add_event_handler("shutdown", _stop_purge)

    # ---- License gateway (Model C2) --------------------------------
    # Enabled when LICENSE_SECRET is set. Every brain endpoint then
    # requires a valid, unexpired, in-quota license key (Bearer token).
    license_secret = os.environ.get("LICENSE_SECRET", "")
    license_enabled = bool(license_secret)
    license_store = (
        LicenseStore(os.environ.get("LICENSE_DB", "data/licenses.db"))
        if license_enabled
        else None
    )
    grace_days = int(os.environ.get("LICENSE_GRACE_DAYS", "7"))

    def _license(
        authorization: str = Header(default=""),
        x_client_bind: str = Header(default=""),
    ) -> LicenseContext | None:
        """FastAPI dependency: gate a request on a valid license.

        No-op when licensing is disabled (LICENSE_SECRET unset) — useful
        for local/self-hosted single-tenant runs.
        """
        if not license_enabled:
            return None
        token = authorization.removeprefix("Bearer ").strip()
        try:
            return check_request(
                token, license_store, license_secret,
                grace_days=grace_days, bind=x_client_bind or None,
            )
        except LicenseError as e:
            raise HTTPException(
                status_code=e.code,
                detail=e.message,
                headers={"x-license-error": str(e.code)},
            ) from e

    def _meter(ctx: LicenseContext | None, cost_usd: float) -> None:
        """Charge a session's model cost back to the client's license."""
        if ctx and license_store:
            license_store.record_usage(ctx.key_id, cost_usd)

    # Payment webhooks (auto issue/renew + Telegram delivery).
    # Requires licensing ON + WEBHOOK_SHARED_SECRET set.
    webhook_enabled = license_enabled and bool(
        os.environ.get("WEBHOOK_SHARED_SECRET", "")
    )
    if webhook_enabled:
        from .webhook import build_webhook_router
        app.include_router(build_webhook_router(license_store, license_secret))

    def _require_key() -> None:
        if not cfg.provider.api_key:
            hint = f" (missing env: {', '.join(missing_env)})" if missing_env else ""
            raise HTTPException(
                status_code=500, detail="provider.api_key is empty" + hint
            )

    # ------------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "modes": list(cfg.modes),
            "default_mode": cfg.default_mode,
            "api_key_configured": bool(cfg.provider.api_key),
            "license_enabled": license_enabled,
            "webhook_enabled": webhook_enabled,
            "passthrough_modes": list(cfg.passthrough),
            "session_retention_days": retention_days,
        }

    @app.post("/api/council")
    async def api_council(req: CouncilRequest, lic: LicenseContext | None = Depends(_license)):
        _require_key()
        try:
            result = await pipeline.run(
                req.question,
                req.mode,
                force=req.force,
                stream_final=False,
                review_override=_review_override(req.skip_review),
            )
        except Exception as e:
            raise _map_error(e) from e
        _meter(lic, result.cost_est)
        out = result.public_dict()
        if lic:
            out["license"] = {"days_left": round(lic.days_left, 1),
                              "quota_left_usd": lic.quota_left_usd}
        return out

    @app.post("/api/council/stream")
    async def api_council_stream(req: CouncilRequest, request: Request,
                                 lic: LicenseContext | None = Depends(_license)):
        _require_key()
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict) -> None:
            await queue.put(ev)

        async def runner() -> None:
            try:
                result = await pipeline.run(
                    req.question,
                    req.mode,
                    force=req.force,
                    emit=emit,
                    stream_final=True,
                    review_override=_review_override(req.skip_review),
                )
                _meter(lic, result.cost_est)
            except (CouncilError, ProviderError) as e:
                await queue.put({"type": "error", "message": str(e)})
            except Exception as e:  # pragma: no cover - defensive
                await queue.put({"type": "error", "message": f"internal error: {e}"})
            finally:
                await queue.put(None)

        async def gen():
            task = asyncio.create_task(runner())
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if ev is None:
                        break
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/models")
    async def api_models():
        _require_key()
        try:
            payload = await provider.get_models()
        except ProviderError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        priced = price_book.update_from_models_payload(payload)
        config_check: dict[str, bool | None] = {}
        for mode in cfg.modes.values():
            for ref in list(mode.advisors) + [mode.chairman]:
                config_check[ref.model] = price_book.in_catalog(ref.model)
        return {
            "priced_models": priced,
            "catalog_size": len(price_book.catalog_ids),
            "config_models_in_catalog": config_check,
            "catalog": payload,
        }

    @app.get("/api/usage")
    async def api_usage():
        spent = guard.windows()
        caps = guard.caps()
        return {
            "windows": {
                k: {
                    "spent": round(spent[k], 4),
                    "cap": caps[k],
                    "pct": round(100 * spent[k] / caps[k], 1) if caps[k] else None,
                }
                for k in caps
            },
            "sessions_24h": ledger.session_count_since(86400),
            "mode_estimates": {
                m: round(guard.estimate_mode_cost(m), 4) for m in cfg.modes
            },
            "note": (
                "Local estimates from the ledger; the provider's own usage "
                "accounting is authoritative."
            ),
        }

    @app.get("/api/sessions")
    async def api_sessions(limit: int = 50):
        return {"sessions": storage.list(limit=limit)}

    @app.get("/api/sessions/{session_id}")
    async def api_session(session_id: str):
        data = storage.get(session_id)
        if data is None:
            raise HTTPException(status_code=404, detail="session not found")
        return data

    # ------------------------------------------------------------------
    # OpenAI-compatible facade
    # ------------------------------------------------------------------

    async def _passthrough(model_name: str, body: dict,
                           lic: LicenseContext | None):
        """Single-model chat for everyday persona traffic.

        One cheap call (~$0.0001–0.0005/exchange with a flash-class model)
        instead of a 2N+1 council session — the difference between a $3
        monthly quota lasting 3 days and lasting all month. Full message
        history is forwarded, unlike council modes (which take the last
        user question by design).
        """
        ref = cfg.passthrough[model_name]
        messages = body.get("messages") or []
        if not messages:
            raise HTTPException(status_code=400, detail="no messages")
        max_tokens = max(1, min(int(body.get("max_tokens") or 1024), 4000))
        temperature = float(body.get("temperature") or 0.7)
        created = int(time.time())
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        def _account(usage) -> None:
            cost, _known = price_book.estimate(
                ref.model, usage.input_tokens, usage.output_tokens
            )
            try:
                ledger.record(
                    model=ref.model, stage="passthrough",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost=cost, session_id=cid,
                )
            except Exception:  # pragma: no cover - accounting must not crash
                pass
            _meter(lic, cost)

        if not body.get("stream"):
            try:
                text, usage = await provider.chat_raw(
                    ref, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
            except ProviderError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e
            _account(usage)
            return {
                "id": cid, "object": "chat.completion", "created": created,
                "model": model_name,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }

        async def gen():
            def chunk(delta: dict, finish: str | None = None) -> str:
                return "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": finish}],
                }, ensure_ascii=False) + "\n\n"

            yield chunk({"role": "assistant", "content": ""})
            try:
                async for kind, payload in provider.stream_raw(
                    ref, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                ):
                    if kind == "token":
                        yield chunk({"content": payload})
                    elif kind == "usage":
                        _account(payload)
            except ProviderError as e:
                yield "data: " + json.dumps(
                    {"error": str(e)}, ensure_ascii=False) + "\n\n"
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/chat/completions")
    async def facade(request: Request, lic: LicenseContext | None = Depends(_license)):
        _require_key()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")

        model_name = body.get("model") or "council"
        if model_name in cfg.passthrough:
            return await _passthrough(model_name, body, lic)
        if model_name in FACADE_MODES:
            mode = FACADE_MODES[model_name] or cfg.default_mode
        elif model_name in cfg.modes:
            mode = model_name
        else:
            known = ", ".join(
                list(cfg.passthrough) + list(FACADE_MODES) + list(cfg.modes)
            )
            raise HTTPException(
                status_code=404,
                detail=f"unknown model {model_name!r}; use one of: {known}",
            )
        if mode not in cfg.modes:
            raise HTTPException(
                status_code=404, detail=f"mode {mode!r} not configured"
            )

        question = _last_user_text(body.get("messages") or [])
        if not question:
            raise HTTPException(status_code=400, detail="no user message found")

        created = int(time.time())
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        stream = bool(body.get("stream"))

        if not stream:
            try:
                result = await pipeline.run(question, mode, stream_final=False)
            except Exception as e:
                raise _map_error(e) from e
            _meter(lic, result.cost_est)
            meta = result.public_dict()
            meta.pop("final", None)
            return {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.final},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": result.usage.input_tokens,
                    "completion_tokens": result.usage.output_tokens,
                    "total_tokens": result.usage.input_tokens
                    + result.usage.output_tokens,
                },
                "council_metadata": meta,
            }

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict) -> None:
            await queue.put(ev)

        async def runner() -> None:
            try:
                result = await pipeline.run(
                    question, mode, emit=emit, stream_final=True
                )
                _meter(lic, result.cost_est)
                meta = result.public_dict()
                meta.pop("final", None)
                await queue.put(
                    {
                        "type": "_final",
                        "metadata": meta,
                        "usage": {
                            "prompt_tokens": result.usage.input_tokens,
                            "completion_tokens": result.usage.output_tokens,
                            "total_tokens": result.usage.input_tokens
                            + result.usage.output_tokens,
                        },
                    }
                )
            except Exception as e:
                await queue.put({"type": "error", "message": str(e)})
            finally:
                await queue.put(None)

        async def gen():
            task = asyncio.create_task(runner())

            def chunk(delta: dict, finish: str | None = None, extra: dict | None = None) -> str:
                payload = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}
                    ],
                }
                if extra:
                    payload.update(extra)
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant", "content": ""})
            try:
                while True:
                    ev = await queue.get()
                    if ev is None:
                        break
                    etype = ev.get("type")
                    if etype == "stage3_token":
                        yield chunk({"content": ev["token"]})
                    elif etype == "_final":
                        yield chunk(
                            {},
                            "stop",
                            {"usage": ev["usage"], "council_metadata": ev["metadata"]},
                        )
                        yield "data: [DONE]\n\n"
                    elif etype == "error":
                        msg = ev.get("message", "unknown")
                        yield chunk({"content": f"\n\n[council error: {msg}]"})
                        yield chunk({}, "stop")
                        yield "data: [DONE]\n\n"
                    else:
                        # progress events become SSE comments (keep-alives)
                        yield f": {etype}\n\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
