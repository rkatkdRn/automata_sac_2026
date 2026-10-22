"""Exact, vectorized Colonel Blotto rules (R1, R5-R8, R10-R17).

This module has no database, clock, random strategy generation or network access.
Fractions are used for ordering and percentile boundaries; floats are display only.
"""
from __future__ import annotations

from fractions import Fraction
from functools import cmp_to_key
from itertools import combinations
from typing import Any, Mapping, Sequence

import numpy as np

FRONTS = 5
TOTAL = 101


def validate_strategy(strategy: Any) -> bool:
    """JSON booleans and numeric strings are not soldiers, despite Python bool<int."""
    return (
        isinstance(strategy, (list, tuple))
        and len(strategy) == FRONTS
        and all(type(x) is int and 0 <= x <= TOTAL for x in strategy)
        and sum(strategy) == TOTAL
    )


is_valid = validate_strategy


def strategy_of(row: Mapping[str, Any]) -> list[int]:
    return list(row["strategy"]) if "strategy" in row else [row[f"l{i}"] for i in range(1, 6)]


def lanes(a: Sequence[int], b: Sequence[int]) -> tuple[int, int]:
    return sum(x > y for x, y in zip(a, b)), sum(x < y for x, y in zip(a, b))


def duel_points(a: Sequence[int], b: Sequence[int]) -> int:
    wins, losses = lanes(a, b)
    return 2 if wins > losses else 0 if wins < losses else 1


def score(strategy: Sequence[int], pool: Sequence[Sequence[int]]) -> int:
    return int(score_vector(strategy, pool).sum())


def score_vector(strategy: Sequence[int], pool: Sequence[Sequence[int]]) -> np.ndarray:
    """Individual integer points against a batch; also used by incremental caching."""
    if len(pool) == 0:
        return np.zeros(0, dtype=np.int8)
    opponents = np.asarray(pool, dtype=np.int16)
    current = np.asarray(strategy, dtype=np.int16)
    wins = (current > opponents).sum(axis=1)
    losses = (current < opponents).sum(axis=1)
    return np.where(wins > losses, 2, np.where(wins < losses, 0, 1)).astype(np.int8)


def qualified(points: int, opponents: int, threshold_bp: int) -> bool:
    return opponents > 0 and points * 10000 >= threshold_bp * 2 * opponents


def full_matrix(strategies: Sequence[Sequence[int]]) -> np.ndarray:
    """N x N integer score matrix. Per-lane broadcasting avoids N x N x 5 copies."""
    n = len(strategies)
    if not n:
        return np.zeros((0, 0), dtype=np.int8)
    s = np.asarray(strategies, dtype=np.int16)
    balance = np.zeros((n, n), dtype=np.int8)
    for lane in range(FRONTS):
        v = s[:, lane]
        balance += (v[:, None] > v[None, :]).astype(np.int8)
        balance -= (v[:, None] < v[None, :]).astype(np.int8)
    return (np.sign(balance) + 1).astype(np.int8)


def analyze_pool(rows: Sequence[Mapping[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Recompute every row against all other people; each seed is a distinct rival."""
    n = len(rows)
    if n == 0:
        return {}
    matrix = full_matrix([strategy_of(r) for r in rows])
    groups: dict[Any, list[int]] = {}
    for i, row in enumerate(rows):
        pid = row.get("participant_id")
        # NULL seed participants must not all be treated as the same person.
        group = ("seed", i) if pid is None else ("person", pid)
        groups.setdefault(group, []).append(i)
    opponent_counts = np.full(n, n, dtype=np.int32)
    for indices in groups.values():
        matrix[np.ix_(indices, indices)] = 0
        opponent_counts[indices] -= len(indices)
    totals = matrix.sum(axis=1, dtype=np.int32)
    return {
        row["id"]: {
            "points": int(totals[i]),
            "opponents": int(opponent_counts[i]),
            "win_rate": int(totals[i]) / (2 * int(opponent_counts[i])) if opponent_counts[i] else 0.0,
        }
        for i, row in enumerate(rows)
    }


def rate_fraction(result: Mapping[str, Any]) -> Fraction:
    return Fraction(int(result["points"]), 2 * int(result["opponents"])) if result["opponents"] else Fraction(0)


def _ordering(row: Mapping[str, Any], analysis: Mapping[Any, Mapping[str, Any]]) -> tuple:
    return (-rate_fraction(analysis[row["id"]]), str(row.get("submitted_at", "")), str(row["id"]))


def rank_rows(
    rows: Sequence[Mapping[str, Any]],
    scope_day: int | None = None,
    analysis: Mapping[Any, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """R13: best eligible strategy per person, exact ties resolved by earlier time."""
    analysis = analyze_pool(rows) if analysis is None else analysis
    def compare(a, b):
        sa, sb = analysis[a["id"]], analysis[b["id"]]
        pa, na = int(sa["points"]), int(sa["opponents"])
        pb, nb = int(sb["points"]), int(sb["opponents"])
        if not na:
            pa, na = 0, 1
        if not nb:
            pb, nb = 0, 1
        delta = pa * nb - pb * na
        if delta:
            return -1 if delta > 0 else 1
        ta = (str(a.get("submitted_at", "")), str(a["id"]))
        tb = (str(b.get("submitted_at", "")), str(b["id"]))
        return (ta > tb) - (ta < tb)

    # Select a person's best strategy before sorting, reducing a three-attempt
    # event's sorting work to the number of participants without rounding rates.
    best = {}
    for row in rows:
        pid = row.get("participant_id")
        if pid is None or row.get("is_seed", False) or not row.get("is_ranked", True):
            continue
        if scope_day is not None and row.get("day_no") != scope_day:
            continue
        if pid not in best or compare(row, best[pid]) < 0:
            best[pid] = row
    eligible = sorted(best.values(), key=cmp_to_key(compare))
    result = []
    for row in eligible:
        pid = row["participant_id"]
        anonymous = f"익명{str(pid)[:6]}"
        nickname = anonymous if row.get("name_hidden") else row.get("nickname", anonymous)
        result.append({
            "rank": len(result) + 1,
            "participant_id": pid,
            "submission_id": row["id"],
            "nickname": nickname,
            "strategy": strategy_of(row),
            "submitted_at": row.get("submitted_at"),
            "day_no": row.get("day_no"),
            **analysis[row["id"]],
        })
    return result


def find_cycle(ranked: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    """Return A→B→C→A with the smallest rank sum, considering the top 30 only."""
    top = ranked[:30]
    if len(top) < 3:
        return None
    m = full_matrix([strategy_of(r) for r in top])
    best = None
    best_key = None
    for i, j, k in combinations(range(len(top)), 3):
        if m[i, j] == 2 and m[j, k] == 2 and m[k, i] == 2:
            order = (i, j, k)
        elif m[i, k] == 2 and m[k, j] == 2 and m[j, i] == 2:
            order = (i, k, j)
        else:
            continue
        key = (sum(int(top[x].get("rank", x + 1)) for x in order), i, j, k)
        if best_key is None or key < best_key:
            best, best_key = order, key
    return [dict(top[i]) for i in best] if best is not None else None


def nemesis(
    strategy: Sequence[int],
    rows: Sequence[Mapping[str, Any]],
    participant_id: Any = None,
    analysis: Mapping[Any, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    analysis = analyze_pool(rows) if analysis is None else analysis
    if not rows:
        return None
    strategies = np.asarray([strategy_of(row) for row in rows], dtype=np.int16)
    current = np.asarray(strategy, dtype=np.int16)
    won_counts = (current > strategies).sum(axis=1)
    lost_counts = (current < strategies).sum(axis=1)
    candidates = []
    for index in np.flatnonzero(lost_counts > won_counts):
        row = rows[index]
        if participant_id is not None and row.get("participant_id") == participant_id:
            continue
        opponent = strategies[index].tolist()
        key = (-rate_fraction(analysis[row["id"]]), -int(lost_counts[index]), str(row.get("submitted_at", "")), str(row["id"]))
        candidates.append((key, opponent))
    if not candidates:
        return None
    _, opponent = min(candidates)
    return {"strategy": opponent, "lost_lanes": [i + 1 for i, (a, b) in enumerate(zip(strategy, opponent)) if a < b]}


def threshold_from_results(results: Sequence[Mapping[str, Any]]) -> int:
    """Exact linear 85th percentile, then ceil to whole percent (100 basis points)."""
    values = sorted(rate_fraction(r) for r in results if r["opponents"] > 0)
    if not values:
        raise ValueError("POOL_TOO_SMALL")
    position = Fraction((len(values) - 1) * 85, 100)
    floor = position.numerator // position.denominator
    fraction = position - floor
    percentile = values[floor] + (values[min(floor + 1, len(values) - 1)] - values[floor]) * fraction
    percent = percentile * 100
    return ((percent.numerator + percent.denominator - 1) // percent.denominator) * 100


def threshold_suggestion(
    rows: Sequence[Mapping[str, Any]], analysis: Mapping[Any, Mapping[str, Any]] | None = None,
) -> int:
    analysis = analyze_pool(rows) if analysis is None else analysis
    return threshold_from_results(list(analysis.values()))


rankings = rank_rows
