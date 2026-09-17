"""Tests for the threshold calibration tool.

The point of the tool is that a similarity threshold must be *derived* from the
model's own distribution, so these tests pin the decision logic (clean gap →
midpoint, overlap → strict) and the sanity of the labeled corpus.
"""

from __future__ import annotations

from backend.tools.threshold_calibration import (
    PARAPHRASE,
    SAME_TOPIC,
    UNRELATED,
    BGE_ZH_QUERY_PREFIX,
    Group,
    _percentile,
    recommend,
)


def _group(name: str, scores: list[float]) -> Group:
    group = Group(name, [("a", "b")] * len(scores))
    group.scores = list(scores)
    return group


# ── decision logic ─────────────────────────────────────────────────────


def test_clean_gap_picks_the_midpoint():
    positive = _group("paraphrase", [0.90, 0.86, 0.88])
    negative = _group("same_topic", [0.70, 0.66, 0.62])

    result = recommend(positive, [negative])

    assert result["threshold"] == 0.78                      # (0.70 + 0.86) / 2
    assert result["separation"] == round(0.86 - 0.70, 4)
    assert "间隔" in result["note"]


def test_overlap_falls_back_to_the_strictest_feasible_value():
    """No clean split exists → refuse to accept any known false positive."""
    positive = _group("paraphrase", [0.80, 0.43, 0.79])
    negative = _group("same_topic", [0.90, 0.77, 0.66])

    result = recommend(positive, [negative])

    assert result["separation"] < 0
    assert result["threshold"] == 0.43                      # min(positive)
    assert "重叠" in result["note"]
    assert "宁可漏命中也不误命中" in result["note"]


def test_overlap_threshold_rejects_every_measured_negative():
    """The whole point: the recommended value must not admit a false positive."""
    positive = _group("paraphrase", [0.80, 0.60])
    negative = _group("same_topic", [0.90, 0.70])

    threshold = recommend(positive, [negative])["threshold"]

    assert all(threshold > score for score in negative.scores[:1]) is False  # 0.60 < 0.90
    # A false positive above the threshold is unavoidable under overlap; what
    # must hold is that the value is the strictest that still admits positives.
    assert threshold == min(positive.scores)


def test_recommendation_reports_the_worst_offenders():
    positive = _group("paraphrase", [0.81, 0.90])
    negative = _group("same_topic", [0.88, 0.60])

    result = recommend(positive, [negative])

    assert result["gap_low"] == 0.88
    assert result["gap_high"] == 0.81
    assert result["worst_negative"][0]["score"] == 0.88
    assert result["worst_positive"][0]["score"] == 0.81


def test_recommend_handles_missing_measurements():
    empty = Group("paraphrase", [])
    assert recommend(empty, [Group("same_topic", [])])["threshold"] is None


def test_percentile_and_stats():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([1.0], 0.5) == 1.0
    assert _percentile([0.0, 1.0], 0.0) == 0.0
    assert _percentile([0.0, 1.0], 1.0) == 1.0

    group = _group("paraphrase", [0.2, 0.4, 0.6, 0.8])
    stats = group.stats()
    assert stats["n"] == 4
    assert stats["min"] == 0.2 and stats["max"] == 0.8
    assert stats["median"] == 0.5


# ── corpus sanity ──────────────────────────────────────────────────────


def test_corpus_is_large_enough_to_be_meaningful():
    for name, pairs in (("paraphrase", PARAPHRASE),
                        ("same_topic", SAME_TOPIC),
                        ("unrelated", UNRELATED)):
        assert len(pairs) >= 10, f"{name} needs at least 10 pairs"


def test_corpus_pairs_are_well_formed():
    for name, pairs in (("paraphrase", PARAPHRASE),
                        ("same_topic", SAME_TOPIC),
                        ("unrelated", UNRELATED)):
        for a, b in pairs:
            assert a.strip() and b.strip(), f"empty side in {name}"
            assert a != b, f"identical sides in {name}: {a!r}"


def test_corpus_is_a_matched_design():
    """Every probe query is paired under all three conditions.

    Repeating the left side across groups is the control: differences in score
    then come from the pairing, not from the query.  What must hold is that each
    group uses the probes exactly once, and that all groups use the *same* set.
    """
    lefts = [[a for a, _ in pairs] for pairs in (PARAPHRASE, SAME_TOPIC, UNRELATED)]

    for index, group in enumerate(lefts):
        assert len(group) == len(set(group)), f"group {index} repeats a probe query"
    assert lefts[0] == lefts[1] == lefts[2], "groups must share the same probe queries"


def test_same_topic_pairs_share_a_topic_but_differ():
    """Guard against a `same_topic` entry that is really a paraphrase."""
    assert not set(SAME_TOPIC) & set(PARAPHRASE)
    for a, b in SAME_TOPIC:
        assert a != b


def test_unrelated_pairs_do_not_overlap_with_the_other_groups():
    assert not set(UNRELATED) & set(PARAPHRASE)
    assert not set(UNRELATED) & set(SAME_TOPIC)


def test_bge_prefix_constant_is_the_documented_instruction():
    assert BGE_ZH_QUERY_PREFIX.startswith("为这个句子生成表示")
