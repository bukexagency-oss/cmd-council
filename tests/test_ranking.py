"""Tests for FINAL RANKING parsing and aggregation."""
from cmd_council.ranking import (
    aggregate_rankings,
    parse_ranking,
    parse_ranking_json,
)

LABELS = ["Response A", "Response B", "Response C"]


def test_parse_clean_block():
    text = (
        "Response A is thorough. Response C is shallow.\n\n"
        "FINAL RANKING:\n1. Response A\n2. Response B\n3. Response C\n"
    )
    assert parse_ranking(text, LABELS) == ["Response A", "Response B", "Response C"]


def test_parse_messy_block():
    text = (
        "some analysis...\n\nfinal ranking:\n"
        "1) **Response C**\n2) response a\n3 - Response B\n"
    )
    assert parse_ranking(text, LABELS) == ["Response C", "Response A", "Response B"]


def test_parse_filters_invalid_labels_and_dupes():
    text = (
        "FINAL RANKING:\n1. Response Z\n2. Response B\n3. Response B\n4. Response A\n"
    )
    assert parse_ranking(text, LABELS) == ["Response B", "Response A"]


def test_parse_missing_block_returns_none():
    assert parse_ranking("I refuse to rank anything.", LABELS) is None
    assert parse_ranking("", LABELS) is None
    assert parse_ranking("FINAL RANKING:\n1. Response A\n", LABELS) is None  # <2 labels


def test_parse_json_fallback():
    assert parse_ranking_json(
        'Sure: ["Response B", "Response A", "banana"]', LABELS
    ) == ["Response B", "Response A"]
    assert parse_ranking_json("no json here", LABELS) is None


def test_aggregate_average_and_sort():
    label_map = {"Response A": "m1", "Response B": "m2", "Response C": "m3"}
    rankings = {
        "m1": ["Response B", "Response A", "Response C"],
        "m2": ["Response B", "Response C", "Response A"],
        "m3": ["Response A", "Response B", "Response C"],
    }
    agg = aggregate_rankings(rankings, label_map)
    assert [item["label"] for item in agg] == ["Response B", "Response A", "Response C"]
    b = agg[0]
    assert b["model"] == "m2"
    assert b["avg_rank"] == round((1 + 1 + 2) / 3, 2)
    assert b["votes"] == 3


def test_aggregate_ignores_missing_votes():
    label_map = {"Response A": "m1", "Response B": "m2"}
    rankings = {"m1": ["Response A", "Response B"], "m2": ["Response B"]}
    # m2's list contains only Response B (invalid short list is caller's
    # concern here) — Response A gets 1 vote, Response B gets 2.
    agg = aggregate_rankings(rankings, label_map)
    by_label = {i["label"]: i for i in agg}
    assert by_label["Response A"]["votes"] == 1
    assert by_label["Response B"]["votes"] == 2
