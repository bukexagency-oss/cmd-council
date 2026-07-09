"""Credit ledger + quota guard (rolling windows, auto-downgrade).

Command Code Pro meters the included monthly credits through two rolling
windows (30% per 5h = $9, 60% per 7d = $18 on the $30/mo quota). This
module keeps a local estimate ledger so cmd-council can warn, downgrade
the council mode, or refuse a session BEFORE burning through a window.

The provider's own accounting is authoritative; this is a local guard,
deliberately conservative (unknown prices fall back to a high estimate).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import AppConfig
from .pricing import PriceBook

# PRD §9 token profile per call (assumptions used for pre-flight estimates).
PROFILE = {
    "stage1": (1000, 700),
    "stage2": (4000, 700),
    "stage3": (7000, 1200),
}

WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 86400


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        model: str,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        session_id: str,
    ) -> None:
        entry = {
            "ts": time.time(),
            "model": model,
            "stage": stage,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": round(cost, 8),
            "session_id": session_id,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def entries(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def cost_since(self, seconds: float) -> float:
        cutoff = time.time() - seconds
        return sum(e.get("cost", 0.0) for e in self.entries() if e.get("ts", 0) >= cutoff)

    def cost_month_to_date(self) -> float:
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()
        return sum(e.get("cost", 0.0) for e in self.entries() if e.get("ts", 0) >= start)

    def session_count_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        return len(
            {
                e.get("session_id")
                for e in self.entries()
                if e.get("ts", 0) >= cutoff and e.get("session_id")
            }
        )


@dataclass
class Decision:
    allowed: bool
    mode: str
    requested: str
    estimated_cost: float = 0.0
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def downgraded(self) -> bool:
        return self.allowed and self.mode != self.requested


class BudgetGuard:
    def __init__(self, cfg: AppConfig, ledger: Ledger, price_book: PriceBook) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.price_book = price_book

    # ---------------- estimates ----------------

    def estimate_mode_cost(self, mode_name: str) -> float:
        """Pre-flight cost estimate for one full session in `mode_name`."""
        mode = self.cfg.modes[mode_name]
        total = 0.0
        for advisor in mode.advisors:
            total += self.price_book.estimate(advisor.model, *PROFILE["stage1"])[0]
            if mode.review:
                total += self.price_book.estimate(advisor.model, *PROFILE["stage2"])[0]
        total += self.price_book.estimate(mode.chairman.model, *PROFILE["stage3"])[0]
        return total

    def windows(self) -> dict[str, float]:
        return {
            "5h": self.ledger.cost_since(WINDOW_5H),
            "7d": self.ledger.cost_since(WINDOW_7D),
            "month": self.ledger.cost_month_to_date(),
        }

    def caps(self) -> dict[str, float]:
        b = self.cfg.budget
        return {"5h": b.window_5h, "7d": b.window_7d, "month": b.monthly_credits}

    # ---------------- pre-flight ----------------

    def preflight(self, requested_mode: str, *, force: bool = False) -> Decision:
        """Pick the mode to run: requested if budget allows, else downgrade.

        Policy: first mode in the downgrade chain that keeps every window
        under the soft threshold wins silently; failing that, the first
        under the hard threshold wins with a warning; failing that the
        session is refused (unless force=True).
        """
        chain = self.cfg.downgrade_chain(requested_mode)
        if not chain:
            return Decision(
                allowed=False,
                mode=requested_mode,
                requested=requested_mode,
                reason=f"unknown mode: {requested_mode!r}",
            )

        spent = self.windows()
        caps = self.caps()
        soft = self.cfg.budget.soft_stop_pct / 100.0
        hard = self.cfg.budget.hard_stop_pct / 100.0

        def fits(est: float, frac: float) -> bool:
            return all(spent[k] + est <= caps[k] * frac for k in caps)

        estimates = {m: self.estimate_mode_cost(m) for m in chain}

        for m in chain:
            if fits(estimates[m], soft):
                d = Decision(
                    allowed=True, mode=m, requested=requested_mode,
                    estimated_cost=estimates[m],
                )
                if m != requested_mode:
                    d.warnings.append(
                        f"budget guard: downgraded {requested_mode} -> {m} "
                        f"(soft threshold {self.cfg.budget.soft_stop_pct:.0f}% reached; "
                        f"windows: " + ", ".join(
                            f"{k}=${spent[k]:.2f}/${caps[k]:.2f}" for k in caps
                        ) + ")"
                    )
                return d

        for m in chain:
            if fits(estimates[m], hard):
                d = Decision(
                    allowed=True, mode=m, requested=requested_mode,
                    estimated_cost=estimates[m],
                )
                d.warnings.append(
                    f"budget guard: above soft threshold "
                    f"({self.cfg.budget.soft_stop_pct:.0f}%), running {m} anyway; "
                    "consider waiting for the rolling window to reset"
                )
                if m != requested_mode:
                    d.warnings.append(
                        f"budget guard: downgraded {requested_mode} -> {m}"
                    )
                return d

        if force:
            m = chain[-1]
            return Decision(
                allowed=True, mode=m, requested=requested_mode,
                estimated_cost=estimates[m],
                warnings=[
                    "budget guard: hard threshold exceeded but force=True — "
                    "this may hit the provider's own rolling-window limits"
                ],
            )

        return Decision(
            allowed=False, mode=requested_mode, requested=requested_mode,
            reason=(
                f"budget hard stop ({self.cfg.budget.hard_stop_pct:.0f}%): "
                + ", ".join(f"{k}=${spent[k]:.2f}/${caps[k]:.2f}" for k in caps)
                + ". Wait for the rolling window to reset, or pass force=true."
            ),
        )
