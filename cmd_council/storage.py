"""Flat-file JSON persistence for council sessions (karpathy-style).

Improvement over the original: label_map and aggregate ranking metadata
are persisted alongside the stage outputs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .models import CouncilResult


class SessionStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, result: CouncilResult) -> Path:
        path = self.dir / f"{result.id}.json"
        path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def purge_older_than(self, days: int) -> int:
        """Delete session files older than `days` (ToS/PDP retention).
        Returns the number of files removed. days<=0 disables purging."""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        for path in self.dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def get(self, session_id: str) -> dict | None:
        path = self.dir / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list(self, limit: int = 50) -> list[dict]:
        files = sorted(
            self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:limit]
        out: list[dict] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "id": data.get("id"),
                    "created_at": data.get("created_at"),
                    "mode": data.get("mode"),
                    "question": (data.get("question") or "")[:120],
                    "cost_est": data.get("cost_est"),
                }
            )
        return out
