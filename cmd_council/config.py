"""Configuration loading for cmd-council (YAML + ${ENV_VAR} expansion)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Downgrade order used by the budget guard (most to least expensive).
MODE_DOWNGRADE_ORDER = ["max", "standard", "eco"]


class ModelRef(BaseModel):
    model: str
    protocol: Literal["openai", "anthropic"] = "openai"


class ModeConfig(BaseModel):
    advisors: list[ModelRef]
    chairman: ModelRef
    review: bool = True

    @field_validator("advisors")
    @classmethod
    def _at_least_two(cls, v: list[ModelRef]) -> list[ModelRef]:
        if len(v) < 2:
            raise ValueError("a council mode needs at least 2 advisors")
        return v


class StageTokens(BaseModel):
    stage1: int = 900
    stage2: int = 800
    stage3: int = 1500


class LimitsConfig(BaseModel):
    max_output_tokens: StageTokens = StageTokens()
    timeout_seconds: float = 120.0
    retries: int = 1
    quorum: int = 2
    max_concurrency: int = 6


class BudgetConfig(BaseModel):
    monthly_credits: float = 30.0
    window_5h: float = 9.0
    window_7d: float = 18.0
    soft_stop_pct: float = 80.0
    hard_stop_pct: float = 95.0


class UtilityConfig(BaseModel):
    title_model: ModelRef | None = None
    ranking_fallback_parser: ModelRef | None = None


class ProviderConfig(BaseModel):
    base_url: str = "https://api.commandcode.ai/provider/v1"
    api_key: str = ""
    zdr: bool = False


class StorageConfig(BaseModel):
    data_dir: str = "data/conversations"
    ledger: str = "data/ledger.jsonl"


class PriceEntry(BaseModel):
    input: float   # $ per 1M input tokens
    output: float  # $ per 1M output tokens


class AppConfig(BaseModel):
    provider: ProviderConfig
    modes: dict[str, ModeConfig]
    # Single-model passthrough modes exposed on the /v1 facade (e.g. "chat",
    # "chat-eco"). Everyday persona traffic should use these — one cheap call
    # instead of a council session. Names shadow FACADE_MODES if they collide.
    passthrough: dict[str, ModelRef] = Field(default_factory=dict)
    limits: LimitsConfig = LimitsConfig()
    budget: BudgetConfig = BudgetConfig()
    utility: UtilityConfig = UtilityConfig()
    storage: StorageConfig = StorageConfig()
    pricing_fallback: dict[str, PriceEntry] = Field(default_factory=dict)
    credit_multipliers: dict[str, float] = Field(default_factory=dict)
    default_mode: str = "standard"

    @field_validator("modes")
    @classmethod
    def _non_empty(cls, v: dict[str, ModeConfig]) -> dict[str, ModeConfig]:
        if not v:
            raise ValueError("at least one council mode must be configured")
        return v

    def downgrade_chain(self, start: str) -> list[str]:
        """Modes to try in order, starting from `start`, cheapest last."""
        if start not in MODE_DOWNGRADE_ORDER:
            return [start] if start in self.modes else []
        idx = MODE_DOWNGRADE_ORDER.index(start)
        chain = [m for m in MODE_DOWNGRADE_ORDER[idx:] if m in self.modes]
        return chain or ([start] if start in self.modes else [])


def _expand_env(value, missing: list[str]):
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            var = m.group(1)
            if var in os.environ:
                return os.environ[var]
            missing.append(var)
            return ""

        return ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, missing) for v in value]
    return value


def load_config(path: str | Path = "council.yaml") -> tuple[AppConfig, list[str]]:
    """Load YAML config. Returns (config, list of missing env var names)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} did not parse to a mapping")
    missing: list[str] = []
    raw = _expand_env(raw, missing)
    cfg = AppConfig.model_validate(raw)
    return cfg, sorted(set(missing))
