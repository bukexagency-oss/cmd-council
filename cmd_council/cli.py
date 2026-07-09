"""cmd-council command-line interface.

    council ask "question" [--mode eco|standard|max] [--json] [--no-stream]
    council serve [--host 127.0.0.1] [--port 8400]
    council models
    council usage
    council validate [--probe]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .budget import BudgetGuard, Ledger
from .config import AppConfig, load_config
from .pipeline import (
    BudgetExceededError,
    CouncilError,
    CouncilPipeline,
    QuorumError,
)
from .pricing import PriceBook
from .provider import CommandCodeProvider, ProviderError
from .storage import SessionStore


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _build(cfg_path: str, *, need_key: bool = True):
    try:
        cfg, missing = load_config(cfg_path)
    except FileNotFoundError:
        _err(f"config not found: {cfg_path}")
    except Exception as e:
        _err(f"config invalid: {e}")
    if need_key and not cfg.provider.api_key:
        hint = f" (missing env: {', '.join(missing)})" if missing else ""
        _err(
            "provider.api_key is empty" + hint + ". Set CMD_API_KEY or edit council.yaml."
        )
    price_book = PriceBook(cfg.pricing_fallback, cfg.credit_multipliers)
    ledger = Ledger(cfg.storage.ledger)
    guard = BudgetGuard(cfg, ledger, price_book)
    provider = CommandCodeProvider(cfg.provider, cfg.limits)
    storage = SessionStore(cfg.storage.data_dir)
    pipeline = CouncilPipeline(cfg, provider, guard, price_book, storage)
    return cfg, provider, price_book, ledger, guard, pipeline


# ----------------------------------------------------------------------
# ask
# ----------------------------------------------------------------------

async def _cmd_ask(args) -> None:
    cfg, provider, _pb, _ledger, _guard, pipeline = _build(args.config)
    quiet = args.json
    state = {"streamed": 0}

    async def emit(ev: dict) -> None:
        if quiet:
            return
        etype = ev["type"]
        if etype == "stage1_start":
            print(
                f"→ Stage 1 · first opinions — {len(ev['models'])} advisors "
                f"({', '.join(ev['models'])}) [mode: {ev['mode']}]",
                file=sys.stderr,
            )
        elif etype == "stage1_complete":
            for r in ev["results"]:
                mark = "✓" if not r["error"] else f"✗ {r['error']}"
                print(f"   {mark} {r['model']}", file=sys.stderr)
        elif etype == "stage2_start":
            print("→ Stage 2 · anonymized peer review…", file=sys.stderr)
        elif etype == "stage2_complete":
            for i, item in enumerate(ev["aggregate_ranking"], 1):
                print(
                    f"   {i}. {item['model']} (avg rank {item['avg_rank']}, "
                    f"{item['votes']} votes)",
                    file=sys.stderr,
                )
        elif etype == "stage3_start":
            print(f"→ Stage 3 · chairman synthesis — {ev['model']}\n", file=sys.stderr)
        elif etype == "stage3_token":
            state["streamed"] += 1
            print(ev["token"], end="", flush=True)
        elif etype == "stage3_fallback":
            state["streamed"] = 0
            print(f"\n[{ev['message']}]\n", file=sys.stderr)
        elif etype == "stage3_complete":
            if state["streamed"] == 0:
                print(ev["final"])
            else:
                print()
        elif etype == "usage":
            print(
                f"\n— usage: {ev['input_tokens']} in / {ev['output_tokens']} out "
                f"≈ ${ev['cost_est']:.4f}",
                file=sys.stderr,
            )

    try:
        result = await pipeline.run(
            args.question,
            args.mode,
            force=args.force,
            emit=emit,
            stream_final=not args.no_stream,
        )
    except BudgetExceededError as e:
        _err(f"budget guard: {e}")
    except QuorumError as e:
        _err(f"quorum: {e}")
    except (CouncilError, ProviderError) as e:
        _err(str(e))
    finally:
        await provider.aclose()

    if args.json:
        print(json.dumps(result.public_dict(), ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------
# serve
# ----------------------------------------------------------------------

def _cmd_serve(args) -> None:
    try:
        import uvicorn
    except ImportError:
        _err("uvicorn is not installed (pip install uvicorn)")
    from .server import create_app

    app = create_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


# ----------------------------------------------------------------------
# models
# ----------------------------------------------------------------------

async def _cmd_models(args) -> None:
    cfg, provider, price_book, *_ = _build(args.config)
    try:
        payload = await provider.get_models()
    except ProviderError as e:
        await provider.aclose()
        _err(str(e))
    priced = price_book.update_from_models_payload(payload)
    await provider.aclose()

    print(
        f"{len(price_book.catalog_ids)} models in live catalog; "
        f"prices parsed for {priced}."
    )
    for model_id in price_book.catalog_ids:
        price, known = price_book.price_for(model_id)
        tag = (
            f"${price['input']:g}/M in · ${price['output']:g}/M out"
            if known
            else "price unknown (using fallback/default)"
        )
        print(f"  - {model_id}  ({tag})")

    print("\nConfigured model check (align council.yaml with the IDs above):")
    seen: set[tuple[str, str]] = set()
    for mode_name, mode in cfg.modes.items():
        for ref in list(mode.advisors) + [mode.chairman]:
            if (mode_name, ref.model) in seen:
                continue
            seen.add((mode_name, ref.model))
            status = price_book.in_catalog(ref.model)
            mark = "✓" if status else ("✗ NOT FOUND" if status is False else "?")
            print(f"  [{mode_name}] {ref.model}: {mark}")


# ----------------------------------------------------------------------
# usage
# ----------------------------------------------------------------------

def _cmd_usage(args) -> None:
    cfg, provider, _pb, ledger, guard, _pipeline = _build(args.config, need_key=False)
    spent = guard.windows()
    caps = guard.caps()
    print("Budget windows (local estimates; provider accounting is authoritative):")
    for k in ("5h", "7d", "month"):
        pct = 100 * spent[k] / caps[k] if caps[k] else 0.0
        print(f"  {k:>5}: ${spent[k]:.3f} / ${caps[k]:.2f}  ({pct:.1f}%)")
    print(f"  sessions in last 24h: {ledger.session_count_since(86400)}")
    print("\nEstimated cost per session:")
    for m in cfg.modes:
        print(f"  {m}: ≈ ${guard.estimate_mode_cost(m):.4f}")
    asyncio.run(provider.aclose())


# ----------------------------------------------------------------------
# validate (Phase 0 helper)
# ----------------------------------------------------------------------

async def _cmd_validate(args) -> None:
    try:
        cfg, missing = load_config(args.config)
    except Exception as e:
        _err(f"config invalid: {e}")

    ok = True
    print(f"✓ config parsed — modes: {', '.join(cfg.modes)} (default: {cfg.default_mode})")
    if missing:
        print(f"⚠ missing env vars: {', '.join(missing)}")

    price_book = PriceBook(cfg.pricing_fallback, cfg.credit_multipliers)
    ledger = Ledger(cfg.storage.ledger)
    guard = BudgetGuard(cfg, ledger, price_book)
    print("✓ pre-flight session estimates (fallback prices, PRD assumption A2):")
    for m in cfg.modes:
        print(f"    {m}: ≈ ${guard.estimate_mode_cost(m):.4f}/session")

    if not cfg.provider.api_key:
        print("✗ api key empty — set CMD_API_KEY to run the online checks")
        sys.exit(1)

    provider = CommandCodeProvider(cfg.provider, cfg.limits)
    try:
        try:
            payload = await provider.get_models()
            priced = price_book.update_from_models_payload(payload)
            print(
                f"✓ GET /models OK — {len(price_book.catalog_ids)} models, "
                f"{priced} with parsed prices"
            )
            for mode_name, mode in cfg.modes.items():
                for ref in list(mode.advisors) + [mode.chairman]:
                    status = price_book.in_catalog(ref.model)
                    if status is False:
                        ok = False
                        print(
                            f"✗ [{mode_name}] {ref.model} NOT in live catalog — "
                            "update council.yaml"
                        )
        except ProviderError as e:
            ok = False
            print(f"✗ GET /models failed: {e}")

        if args.probe and ok:
            ref = cfg.utility.ranking_fallback_parser or cfg.modes[
                cfg.default_mode
            ].advisors[0]
            print(f"→ probe: 3 parallel micro-calls to {ref.model} (tiny cost)…")

            async def one(i: int) -> str:
                try:
                    text, _usage = await provider.chat(
                        ref,
                        system=None,
                        user="Reply with the single word: ok",
                        max_tokens=8,
                        temperature=0.0,
                    )
                    return f"✓ call {i + 1}: {text.strip()[:24]!r}"
                except ProviderError as e:
                    return f"✗ call {i + 1}: {e}"

            for line in await asyncio.gather(*[one(i) for i in range(3)]):
                print("  " + line)
                if line.startswith("✗"):
                    ok = False
    finally:
        await provider.aclose()

    print("\nresult:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="council",
        description="LLM Council on a single Command Code subscription.",
    )
    parser.add_argument("--config", default="council.yaml", help="path to council.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ask = sub.add_parser("ask", help="run one council session")
    p_ask.add_argument("question")
    p_ask.add_argument("--mode", help="eco | standard | max (default from config)")
    p_ask.add_argument("--json", action="store_true", help="print raw JSON result")
    p_ask.add_argument("--force", action="store_true", help="ignore budget hard stop")
    p_ask.add_argument("--no-stream", action="store_true", help="disable stage-3 token streaming")

    p_serve = sub.add_parser("serve", help="run the FastAPI server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8400)

    sub.add_parser("models", help="fetch live catalog + check configured model IDs")
    sub.add_parser("usage", help="show budget windows and per-mode estimates")

    p_val = sub.add_parser("validate", help="Phase 0 checks (config, catalog, probe)")
    p_val.add_argument("--probe", action="store_true", help="run 3 tiny parallel calls")

    args = parser.parse_args(argv)
    if args.cmd == "ask":
        asyncio.run(_cmd_ask(args))
    elif args.cmd == "serve":
        _cmd_serve(args)
    elif args.cmd == "models":
        asyncio.run(_cmd_models(args))
    elif args.cmd == "usage":
        _cmd_usage(args)
    elif args.cmd == "validate":
        asyncio.run(_cmd_validate(args))


if __name__ == "__main__":
    main()
