"""Published T vectors plus independent reference and exact-boundary checks."""
from fractions import Fraction
import importlib.util
from pathlib import Path
import random

import numpy as np
import pytest

from server import engine as e

spec = importlib.util.spec_from_file_location("reference", Path(__file__).with_name("reference_engine.py"))
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def row(i, strategy, participant=None, **extra):
    return {
        "id": i, "participant_id": participant, "strategy": list(strategy),
        "is_seed": participant is None, "is_ranked": participant is not None,
        "nickname": f"닉네임{participant}", "day_no": 1,
        "submitted_at": f"2026-10-30T10:{i // 60:02d}:{i % 60:02d}+09:00", **extra,
    }


def test_T01_reference_pool_score():
    strategy = (30, 25, 20, 15, 11)
    pool = [(20, 20, 20, 20, 21), (101, 0, 0, 0, 0), (0, 25, 25, 25, 26), (25, 25, 25, 13, 13)]
    assert [e.lanes(strategy, p) for p in pool] == [(2, 2), (4, 1), (1, 3), (2, 2)]
    assert [e.duel_points(strategy, p) for p in pool] == [1, 2, 0, 1]
    assert e.score(strategy, pool) == 4
    assert e.score(strategy, pool) / (2 * len(pool)) == 0.5


@pytest.mark.parametrize("a,b,expected", [
    ((20, 20, 20, 20, 21), (21, 20, 20, 20, 20), 1),
    ((25, 25, 25, 13, 13), (25, 25, 25, 13, 13), 1),
    ((101, 0, 0, 0, 0), (0, 25, 25, 25, 26), 0),
    ((30, 25, 20, 15, 11), (40, 39, 20, 1, 1), 1),
    ((50, 50, 1, 0, 0), (0, 0, 34, 34, 33), 0),
])
def test_T02_ties_and_concentration(a, b, expected):
    assert e.duel_points(a, b) == expected


@pytest.mark.parametrize("strategy", [
    [20, 20, 20, 20, 20], [20, 20, 20, 20, 22], [-1, 25, 25, 25, 27],
    [0.0, 25, 25, 25, 26], ["0", 25, 25, 25, 26], [0, 25, 25, 51],
    [0, 0, 25, 25, 25, 26], [True, 25, 25, 25, 25], None, "10100",
    [None, 25, 25, 25, 26], {"a": 101}, [10**1000, 0, 0, 0, 0],
])
def test_T03_invalid_strategies(strategy):
    assert not e.validate_strategy(strategy)


def test_valid_strategy():
    assert e.validate_strategy([0, 25, 25, 25, 26])
    assert e.validate_strategy([101, 0, 0, 0, 0])


def test_T05_all_own_submissions_excluded_and_seeds_distinct():
    rows = [
        row(1, [101, 0, 0, 0, 0]), row(2, [0, 25, 25, 25, 26]),
        row(3, [30, 25, 20, 15, 11], 7), row(4, [20, 20, 20, 20, 21], 7),
        row(5, [10, 10, 10, 10, 61], 8),
    ]
    stats = e.analyze_pool(rows)
    assert stats[1]["opponents"] == 4
    assert stats[2]["opponents"] == 4
    assert stats[3]["opponents"] == 3
    assert stats[4]["opponents"] == 3
    assert stats[5]["opponents"] == 4
    for r in rows:
        rivals = [s["strategy"] for s in rows if s["id"] != r["id"] and (r["participant_id"] is None or s["participant_id"] != r["participant_id"])]
        assert stats[r["id"]]["points"] == sum(2 * reference.duel(r["strategy"], s) for s in rivals)


def test_T06_exact_threshold_boundary():
    assert e.qualified(36, 25, 7200)
    assert not e.qualified(35, 25, 7200)
    assert not e.qualified(0, 0, 0)
    # No float or epsilon belongs in eligibility comparisons.
    assert e.qualified(7200 * 2 * 100000000000, 10000 * 100000000000, 7200)


def test_T09_cycle_published_vector():
    strategies = [(26, 36, 15, 7, 17), (40, 13, 13, 30, 5), (46, 11, 0, 14, 30)]
    rows = [row(i + 1, s, i + 1) for i, s in enumerate(strategies)]
    cycle = e.find_cycle(e.rank_rows(rows))
    assert cycle is not None
    assert [r["strategy"] for r in cycle] == [list(s) for s in strategies]
    for a, b in zip(cycle, cycle[1:] + cycle[:1]):
        assert e.lanes(a["strategy"], b["strategy"]) == (3, 2)


def test_T10_current_pool_best_per_person_seeds_and_staff_excluded():
    strategies = [[101, 0, 0, 0, 0], [0, 25, 25, 25, 26], [20, 20, 20, 20, 21]]
    rows = [row(i + 1, strategies[i % 3]) for i in range(50)]
    rows += [row(51, strategies[0], 1), row(52, strategies[1], 1, day_no=2), row(53, strategies[2], 2), row(54, strategies[1], 3, is_ranked=False)]
    rankings = e.rank_rows(rows)
    assert len(rankings) == 2
    assert {r["participant_id"] for r in rankings} == {1, 2}
    assert all(r["opponents"] >= 50 for r in rankings)
    assert next(r for r in rankings if r["participant_id"] == 1)["submission_id"] == 52
    daily = e.rank_rows(rows, scope_day=1)
    assert next(r for r in daily if r["participant_id"] == 1)["submission_id"] == 51


def test_rank_ties_earlier_submission_and_does_not_leak_private_fields():
    a = row(1, [20, 20, 20, 20, 21], 1, real_name="PRIVATE_NAME", student_id="PRIVATE_ID")
    b = row(2, a["strategy"], 2, name_hidden=True)
    ranks = e.rank_rows([b, a])
    assert ranks[0]["participant_id"] == 1
    assert ranks[1]["nickname"] == "익명2"
    assert "PRIVATE" not in str(ranks)


def test_nemesis_published_vector_hides_nickname():
    strategy = [30, 25, 20, 15, 11]
    rows = [row(i + 1, s) for i, s in enumerate([[20, 20, 20, 20, 21], [101, 0, 0, 0, 0], [0, 25, 25, 25, 26], [25, 25, 25, 13, 13]])]
    assert e.nemesis(strategy, rows) == {"strategy": [0, 25, 25, 25, 26], "lost_lanes": [3, 4, 5]}
    assert e.nemesis([0, 25, 25, 25, 26], [rows[1]]) is None


def test_exact_percentile_ceiling():
    assert e.threshold_from_results([{"points": 36, "opponents": 25}] * 50) == 7200
    assert e.threshold_from_results([{"points": 18, "opponents": 25}] * 50) == 3600
    assert e.threshold_from_results([{"points": 1, "opponents": 3}] * 50) == 1700
    results = [{"points": 1, "opponents": 2}, {"points": 3, "opponents": 4}]
    assert e.threshold_from_results(results) == 3600  # 0.35625 → 0.36
    with pytest.raises(ValueError):
        e.threshold_from_results([])


def test_reference_crosscheck_20000_pairs_and_matrix():
    rng = random.Random(307)
    for _ in range(20000):
        a, b = reference.random_strategy(rng), reference.random_strategy(rng)
        assert e.duel_points(a, b) == int(reference.duel(a, b) * 2)
    strategies = [reference.random_strategy(rng) for _ in range(200)]
    matrix = e.full_matrix(strategies)
    assert np.array_equal(matrix + matrix.T, np.full((200, 200), 2))
    for i in range(200):
        assert int(matrix[i].sum()) == e.score(strategies[i], strategies)


def test_ranking_exact_fraction_not_rounded_display():
    rows = [row(1, [20, 20, 20, 20, 21], 1), row(2, [20, 20, 20, 20, 21], 2)]
    stats = {1: {"points": 1, "opponents": 3, "win_rate": 0.167}, 2: {"points": 2, "opponents": 11, "win_rate": 0.167}}
    assert e.rank_rows(rows, analysis=stats)[0]["participant_id"] == 1
