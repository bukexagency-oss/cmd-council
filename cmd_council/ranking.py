"""Parsing and aggregation of peer-review rankings (karpathy-style)."""
from __future__ import annotations

import json
import re
from collections import defaultdict

FINAL_BLOCK_RE = re.compile(r"FINAL\s+RANKING\s*:?", re.IGNORECASE)
LINE_RE = re.compile(r"\d+\s*[.):\-]\s*\**\s*(Response\s+[A-Z])\b", re.IGNORECASE)


def normalize_label(raw: str) -> str:
    parts = raw.strip().split()
    return f"Response {parts[-1].upper()}" if parts else ""


def parse_ranking(text: str, valid_labels: list[str]) -> list[str] | None:
    """Parse a FINAL RANKING block. Returns ordered labels (best first) or None.

    Requires at least 2 distinct valid labels to count as a usable ranking.
    """
    if not text:
        return None
    m = FINAL_BLOCK_RE.search(text)
    tail = text[m.end():] if m else text
    found = [normalize_label(x) for x in LINE_RE.findall(tail)]
    seen: list[str] = []
    valid = set(valid_labels)
    for label in found:
        if label in valid and label not in seen:
            seen.append(label)
    return seen if len(seen) >= 2 else None


def parse_ranking_json(text: str, valid_labels: list[str]) -> list[str] | None:
    """Parse a JSON array of labels (used by the cheap fallback parser)."""
    if not text:
        return None
    try:
        start, end = text.index("["), text.rindex("]") + 1
        arr = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(arr, list):
        return None
    seen: list[str] = []
    valid = set(valid_labels)
    for item in arr:
        if not isinstance(item, str):
            continue
        label = normalize_label(item)
        if label in valid and label not in seen:
            seen.append(label)
    return seen if len(seen) >= 2 else None


def aggregate_rankings(
    rankings: dict[str, list[str]], label_map: dict[str, str]
) -> list[dict]:
    """Average each label's rank position across reviewers.

    Labels absent from a reviewer's list are simply ignored for that
    reviewer (they don't get a penalty position). Returns dicts sorted
    best (lowest average rank) first.
    """
    positions: dict[str, list[int]] = defaultdict(list)
    for ordered in rankings.values():
        for pos, label in enumerate(ordered, start=1):
            positions[label].append(pos)

    items: list[dict] = []
    for label, model in label_map.items():
        pos = positions.get(label, [])
        if not pos:
            continue
        items.append(
            {
                "label": label,
                "model": model,
                "avg_rank": round(sum(pos) / len(pos), 2),
                "votes": len(pos),
            }
        )
    items.sort(key=lambda x: x["avg_rank"])
    return items
