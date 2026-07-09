"""Core result/data types for cmd-council."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class AdvisorAnswer:
    model: str
    text: str = ""
    label: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_est: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


@dataclass
class Review:
    reviewer_model: str
    text: str = ""
    ranking: list[str] | None = None  # ordered labels, best first
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_est: float = 0.0
    error: str | None = None


@dataclass
class AggregateRankItem:
    label: str
    model: str
    avg_rank: float
    votes: int


@dataclass
class CouncilResult:
    question: str
    mode: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    stage1: list[AdvisorAnswer] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    aggregate_ranking: list[AggregateRankItem] = field(default_factory=list)
    final: str = ""
    chairman_model: str = ""
    degraded: list[str] = field(default_factory=list)
    downgraded_from: str | None = None
    warnings: list[str] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_est: float = 0.0
    label_map: dict[str, str] = field(default_factory=dict)  # label -> model

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Shape returned by the API (stable contract, see PRD §10)."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "question": self.question,
            "mode": self.mode,
            "downgraded_from": self.downgraded_from,
            "warnings": self.warnings,
            "final": self.final,
            "chairman": self.chairman_model,
            "stage1": [
                {
                    "model": a.model,
                    "label": a.label,
                    "answer": a.text,
                    "error": a.error,
                }
                for a in self.stage1
            ],
            "stage2": {
                "aggregate_ranking": [asdict(i) for i in self.aggregate_ranking],
                "reviews": [
                    {
                        "reviewer": r.reviewer_model,
                        "ranking": r.ranking,
                        "text": r.text,
                        "error": r.error,
                    }
                    for r in self.reviews
                ],
            },
            "degraded": self.degraded,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cost_est": round(self.cost_est, 6),
            },
        }
