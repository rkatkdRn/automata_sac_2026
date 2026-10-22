"""블로토 채점 엔진 참조 구현 + 설계서에 넣을 수치 검증."""
import itertools
import random

FRONTS = 5
TOTAL = 101


def is_valid(s):
    return (
        len(s) == FRONTS
        and all(isinstance(x, int) and x >= 0 for x in s)
        and sum(s) == TOTAL
    )


def lanes(a, b):
    """(a가 가져간 전선 수, b가 가져간 전선 수). 같은 병력 전선은 아무도 못 가져감."""
    wa = sum(x > y for x, y in zip(a, b))
    wb = sum(y > x for x, y in zip(a, b))
    return wa, wb


def duel(a, b):
    wa, wb = lanes(a, b)
    return 1.0 if wa > wb else 0.0 if wa < wb else 0.5


def win_rate(s, pool):
    return sum(duel(s, o) for o in pool) / len(pool)


def percentile(values, p):
    """선형 보간 백분위 (numpy 기본 방식과 동일)."""
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def random_strategy(rng):
    cuts = sorted(rng.sample(range(TOTAL + FRONTS - 1), FRONTS - 1))
    parts, prev = [], -1
    for c in cuts + [TOTAL + FRONTS - 1]:
        parts.append(c - prev - 1)
        prev = c
    return tuple(parts)


def capacity(premium_rate, mychew=450, premium=95, top_share=0.20):
    """고급 선택률별 수용 인원 (계획서 03절 표 검증용)."""
    # n명 중 top_share가 승률 통과 -> 그 중 premium_rate가 고급, 나머지는 마이쮸
    best = 0
    for n in range(300, 700):
        top = n * top_share
        prem_used = top * premium_rate
        chew_used = (n - top) + top * (1 - premium_rate)
        if chew_used <= mychew and prem_used <= premium:
            best = n
    return best


if __name__ == "__main__":
    # ---- 단위 테스트 벡터 ----
    cases = [
        ((20, 20, 20, 20, 21), (21, 20, 20, 20, 20), 0.5),
        ((101, 0, 0, 0, 0), (0, 25, 25, 25, 26), 0.0),
        ((0, 25, 25, 25, 26), (101, 0, 0, 0, 0), 1.0),
        ((30, 25, 20, 15, 11), (40, 39, 20, 1, 1), 0.5),  # 계획서 예시: 무승부
        ((25, 25, 25, 13, 13), (25, 25, 25, 13, 13), 0.5),  # 완전 동일
        ((50, 50, 1, 0, 0), (0, 0, 34, 34, 33), 0.0),  # 2전선 vs 3전선 -> 패
    ]
    for a, b, exp in cases:
        r = duel(a, b)
        print(a, b, lanes(a, b), r, "OK" if r == exp else "FAIL")
        assert r == exp

    # ---- 가위바위보 순환 후보 탐색 ----
    A = (40, 40, 21, 0, 0)
    found = None
    rng = random.Random(7)
    for _ in range(200000):
        a, b, c = (random_strategy(rng) for _ in range(3))
        if duel(a, b) == 1 and duel(b, c) == 1 and duel(c, a) == 1:
            if min(a) > 0 or True:
                found = (a, b, c)
                # 읽기 쉬운 예시를 찾기 위해 0이 많지 않은 것을 선호
                if sum(x == 0 for x in a + b + c) <= 1:
                    break
    print("순환 예시", found)

    # ---- 무작위 풀 통계 ----
    rng = random.Random(1)
    pool = [random_strategy(rng) for _ in range(200)]
    ties = sum(duel(a, b) == 0.5 for a in pool for b in pool if a is not b)
    n = len(pool) * (len(pool) - 1)
    print("무작위 풀 무승부 비율", round(ties / n, 3))
    rates = [win_rate(s, [o for o in pool if o is not s]) for s in pool]
    print("승률 범위", round(min(rates), 3), round(max(rates), 3),
          "80%ile", round(percentile(rates, 80), 3))

    # ---- 재고 수용 인원 표 검증 ----
    for r in (0.6, 0.8, 0.9, 1.0):
        print("고급 선택률", r, "수용", capacity(r))
    print("첫 제출 통과율 상한(고급 95개/530명) =", round(95 / 530, 3))
