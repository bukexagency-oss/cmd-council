"""Model catalog & price lookups.

Authoritative source: GET {base_url}/models on Command Code. Response
shapes across aggregators vary, so parsing is defensive; when a price
cannot be determined we fall back to the static `pricing_fallback` table
from council.yaml, and finally to a conservative default.

Prices are stored as $ per 1M tokens. "Deals" (credit multipliers, e.g.
DeepSeek V4 Pro 4x) divide the estimated cost — they are promotional and
revocable per Command Code ToS, so treat every estimate as approximate;
the ledger exists to be reconciled against the provider's own usage page.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import PriceEntry

DEFAULT_PRICE = {"input": 1.0, "output": 4.0}  # conservative $/M fallback

_INPUT_KEYS = (
    "input", "input_price", "prompt", "prompt_price",
    "input_cost_per_mtok", "input_per_million",
)
_OUTPUT_KEYS = (
    "output", "output_price", "completion", "completion_price",
    "output_cost_per_mtok", "output_per_million",
)


def _coerce_per_million(value: Any) -> float | None:
    """Normalize a price value to $/1M tokens.

    Heuristic: values < 0.01 are almost certainly $/token (OpenRouter
    style, e.g. "0.000002") -> multiply by 1e6; otherwise assume the
    value is already $/1M.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v * 1_000_000 if v < 0.01 else v


class PriceBook:
    def __init__(
        self,
        fallback: dict[str, PriceEntry] | None = None,
        multipliers: dict[str, float] | None = None,
        cache_path: str | Path = ".cache/models.json",
        ttl_seconds: int = 3600,
    ) -> None:
        self.fallback = {
            k.lower(): {"input": v.input, "output": v.output}
            for k, v in (fallback or {}).items()
        }
        self.multipliers = {k.lower(): float(v) for k, v in (multipliers or {}).items()}
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_seconds
        self.catalog: dict[str, dict[str, float]] = {}  # model_id -> {input, output}
        self.catalog_ids: list[str] = []
        self._load_cache()

    # ---------------- cache ----------------

    def _load_cache(self) -> None:
        try:
            if not self.cache_path.exists():
                return
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) > self.ttl_seconds:
                return
            self.catalog = data.get("catalog", {})
            self.catalog_ids = data.get("ids", [])
        except (OSError, json.JSONDecodeError):
            pass

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": time.time(),
                        "catalog": self.catalog,
                        "ids": self.catalog_ids,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ---------------- catalog ----------------

    def update_from_models_payload(self, payload: Any) -> int:
        """Ingest a GET /models payload (shape-tolerant). Returns #priced."""
        items: list = []
        if isinstance(payload, dict):
            for key in ("data", "models", "items"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        elif isinstance(payload, list):
            items = payload

        priced = 0
        ids: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("model") or item.get("name")
            if not isinstance(model_id, str):
                continue
            ids.append(model_id)
            pricing = item.get("pricing") or item.get("prices") or item
            if not isinstance(pricing, dict):
                continue
            in_p = next(
                (p for k in _INPUT_KEYS if (p := _coerce_per_million(pricing.get(k))) is not None),
                None,
            )
            out_p = next(
                (p for k in _OUTPUT_KEYS if (p := _coerce_per_million(pricing.get(k))) is not None),
                None,
            )
            if in_p is not None and out_p is not None:
                self.catalog[model_id.lower()] = {"input": in_p, "output": out_p}
                priced += 1
        if ids:
            self.catalog_ids = ids
            self._save_cache()
        return priced

    # ---------------- lookups ----------------

    @staticmethod
    def _match(table: dict[str, Any], model: str) -> Any | None:
        key = model.lower()
        if key in table:
            return table[key]
        for k, v in table.items():
            if k in key or key in k:
                return v
        return None

    def price_for(self, model: str) -> tuple[dict[str, float], bool]:
        """Returns ({input, output} $/1M, known?)."""
        hit = self._match(self.catalog, model)
        if hit:
            return hit, True
        hit = self._match(self.fallback, model)
        if hit:
            return hit, True
        return DEFAULT_PRICE, False

    def multiplier_for(self, model: str) -> float:
        hit = self._match(self.multipliers, model)
        return float(hit) if hit else 1.0

    def estimate(self, model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
        """Estimated credit cost ($) for one call. Returns (cost, price_known)."""
        price, known = self.price_for(model)
        raw = (
            input_tokens * price["input"] + output_tokens * price["output"]
        ) / 1_000_000
        return raw / self.multiplier_for(model), known

    def in_catalog(self, model: str) -> bool | None:
        """True/False if a live catalog is loaded, None if catalog unknown."""
        if not self.catalog_ids:
            return None
        key = model.lower()
        return any(key == i.lower() or key in i.lower() for i in self.catalog_ids)
