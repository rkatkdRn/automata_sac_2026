"""API acceptance checks. Run against disposable SQLite, never the event database.

T07 follows final decision Q2: EITHER stops admission at either cap. The older
T07 TOP_CHOICE+BASIC expectation is tested separately under PREMIUM_SOFT.
T17 physical phones, 5 m TV readability and production latency remain rehearsal.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
import asyncio
import json
import os
import random
from pathlib import Path
import sqlite3
import time
import threading
import uuid
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

KST = ZoneInfo("Asia/Seoul")
WIN = [21, 21, 21, 19, 19]
TIE = [20, 20, 20, 20, 21]
LOSE = [101, 0, 0, 0, 0]
PIN = "integration-tests-only-long-pin"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def error(response, code):
    assert response.status_code >= 400, response.text
    assert response.json()["error"]["code"] == code, response.text


@pytest.fixture
def booth(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "event.db"))
    monkeypatch.setenv("ADMIN_PIN", PIN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    postgres_url = os.getenv("BLOTTO_TEST_DATABASE_URL")
    pg_schema = None
    if postgres_url:
        # Explicit test-only opt-in. A fresh schema is used and removed per test;
        # normal DATABASE_URL is intentionally ignored to protect event data.
        import psycopg
        from psycopg import sql
        pg_schema = "blotto_test_" + uuid.uuid4().hex
        with psycopg.connect(postgres_url, autocommit=True) as pg:
            pg.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(pg_schema)))
        parsed = urlsplit(postgres_url)
        query = dict(parse_qsl(parsed.query))
        query["options"] = f"-csearch_path={pg_schema}"
        isolated_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
        monkeypatch.setenv("DATABASE_URL", isolated_url)
    from server import service
    from server.app import create_app
    clock = [datetime(2026, 10, 30, 10, 30, tzinfo=KST)]
    monkeypatch.setattr(service, "now", lambda: clock[0])
    app = create_app()
    stack = ExitStack()
    admin = stack.enter_context(TestClient(app))
    ok(admin.post("/api/admin/login", json={"pin": PIN}))
    # Test fixtures only. Production deliberately has no seed generator.
    ok(admin.post("/api/admin/seeds/import", json={"items": [{"strategy": TIE, "tag": "테스트", "author": "테스트"} for _ in range(50)]}))
    for item, count in [("MYCHEW", 450), ("SESAME", 48), ("ACE", 47)]:
        ok(admin.post("/api/admin/inventory/count", json={"item": item, "count": count, "reason": "테스트 재고"}))

    class Booth:
        def __init__(self):
            self.admin, self.app, self.clock, self.path = admin, app, clock, tmp_path / "event.db"
            self.postgres = bool(postgres_url)

        def client(self):
            return stack.enter_context(TestClient(app))

        def tries(self, n):
            # The booth allows one try a day; multi-try mechanics (renumbering etc.) still hold for any limit.
            monkeypatch.setattr(service, "MAX_ATTEMPTS", n)

        def guest(self, nickname=None):
            client = stack.enter_context(TestClient(app))
            ok(client.post("/api/session", json={"kind": "GUEST", "nickname": nickname or "손님" + uuid.uuid4().hex[:5], "consent": True}))
            return client

        def student(self, name="테스트학생", student_id="00-000", nickname=None):
            client = stack.enter_context(TestClient(app))
            response = client.post("/api/session", json={"kind": "STUDENT", "student_id": student_id, "name": name,
                "nickname": nickname or "별명" + uuid.uuid4().hex[:6], "consent": True})
            return client, response

        def configure(self, *, day=1, premium=40, mychew=188, rule="EITHER"):
            return ok(admin.post("/api/admin/day-config", json={
                "day_no": day, "date": f"2026-{10 if day == 1 else 10 if day == 2 else 11:02d}-{30 if day == 1 else 31 if day == 2 else 1:02d}",
                "sessions": [["10:00", "13:00"], ["14:00", "17:00"]],
                "mychew_cap_cum": mychew, "premium_cap_cum": premium, "close_rule": rule, "reason": "테스트 설정",
            }))

        def open(self, threshold=7200, mode="FIXED"):
            ok(admin.post("/api/admin/threshold", json={"mode": mode, "value_bp": threshold, "source": "MANUAL", "reason": "테스트 임계값"}))
            return ok(admin.post("/api/admin/booth", json={"action": "OPEN"}))

        def submit(self, client, strategy=TIE, request_id=None):
            return client.post("/api/submissions", json={"strategy": strategy, "request_id": request_id or uuid.uuid4().hex})

        def next_day(self):
            ok(admin.post("/api/admin/booth", json={"action": "CLOSE"}))
            self.clock[0] += timedelta(days=1)
            ok(admin.post("/api/admin/login", json={"pin": PIN}))
            return ok(admin.post("/api/admin/day", json={"day_no": 2}))

    try:
        yield Booth()
    finally:
        stack.close()
        if pg_schema:
            with psycopg.connect(postgres_url, autocommit=True) as pg:
                pg.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(pg_schema)))


def test_T03_invalid_does_not_consume_attempt(booth):
    booth.open()
    guest = booth.guest()
    for strategy in [[20] * 5, [-1, 25, 25, 25, 27], [0.0, 25, 25, 25, 26], [True, 25, 25, 25, 25], ["0", 25, 25, 25, 26], [101, 0, 0, 0]]:
        error(booth.submit(guest, strategy), "INVALID_STRATEGY")
    assert ok(guest.get("/api/state"))["attempts_left"] == 1
    assert ok(guest.get("/api/me/submissions"))["items"] == []


def test_T04_T05_T13_one_attempt_next_day_and_self_exclusion(booth):
    booth.open()
    # Students retain their identity across days; guests now expire after 12h.
    guest, response = booth.student()
    ok(response)
    first = ok(booth.submit(guest))
    assert first["attempt_no"] == 1 and first["attempts_left"] == 0 and first["reward"]["kind"] == "BASIC"
    error(booth.submit(guest, WIN), "LIMIT_REACHED")
    assert ok(guest.get("/api/state"))["max_attempts"] == 1
    booth.next_day()
    booth.open()
    again = ok(booth.submit(guest))
    assert again["attempt_no"] == 1 and again["attempts_left"] == 0
    # The player's own day-1 play is in the pool but never an opponent.
    assert again["opponents"] == 50 and again["pool_size"] == 52


def test_T06_threshold_changes_mid_day_apply_to_later_plays_only(booth):
    booth.open(196, mode="LIVE")
    # WIN is 1st of 51 = top 1.97%: misses a 1.96% cut.
    first = ok(booth.submit(booth.guest("변경전"), WIN))
    assert first["reward"]["kind"] == "BASIC" and first["threshold_bp_used"] == 196
    booth.clock[0] += timedelta(minutes=5)
    ok(booth.admin.post("/api/admin/threshold", json={"mode": "LIVE", "value_bp": 1200, "reason": "기준 완화"}))
    dashboard = ok(booth.admin.get("/api/admin/dashboard"))
    assert dashboard["threshold_bp"] == 1200 and not dashboard["threshold_locked"]
    second = ok(booth.submit(booth.guest("변경후"), WIN))
    assert second["reward"]["kind"] == "TOP_CHOICE" and second["threshold_bp_used"] == 1200
    # The earlier play keeps the rule it was judged by.
    log = {r["nickname"]: r for r in ok(booth.admin.get("/api/admin/submissions"))["items"]}
    assert log["변경전"]["entitlement"]["kind"] == "BASIC"
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    booth.clock[0] += timedelta(minutes=5)
    ok(booth.admin.post("/api/admin/threshold", json={"mode": "LIVE", "value_bp": 1000, "reason": "마감 후 변경"}))
    assert ok(booth.admin.get("/api/admin/dashboard"))["threshold_bp"] == 1000
    assert any(a["action"] == "THRESHOLD_SET" and a["reason"] == "기준 완화" for a in ok(booth.admin.get("/api/admin/audit"))["items"])


def test_T06b_day_runs_until_midnight_ignoring_hours(booth):
    booth.open(1200, mode="LIVE")
    board = ok(booth.admin.get("/api/scoreboard"))
    assert board["day_end"] == "2026-10-31T00:00:00+09:00" and board["last_day"] is False
    # Stored hours (10-13, 14-17) no longer matter: lunch time and the evening stay open.
    for hour, minute in ((13, 30), (17, 30), (23, 59)):
        booth.clock[0] = booth.clock[0].replace(hour=hour, minute=minute)
        assert ok(booth.admin.get("/api/state"))["open_now"]
    booth.clock[0] = booth.clock[0].replace(hour=0, minute=0) + timedelta(days=1)
    assert not ok(booth.admin.get("/api/state"))["open_now"]


@pytest.mark.parametrize("rule", ["EITHER", "PREMIUM_SOFT"])
def test_T07_last_premium_concurrent_requests_serialized(booth, rule):
    booth.configure(premium=1, rule=rule)
    booth.open(0)
    clients = [booth.guest("동시참가1"), booth.guest("동시참가2")]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda c: booth.submit(c, WIN), clients))
    success = [r.json() for r in responses if r.status_code == 200]
    assert sum(r["reward"]["kind"] == "TOP_CHOICE" for r in success) == 1
    if rule == "EITHER":
        assert len(success) == 1
        error(next(r for r in responses if r.status_code != 200), "CAP_REACHED")
    else:
        assert len(success) == 2
        assert sum(r["reward"]["kind"] == "BASIC" for r in success) == 1
    dashboard = ok(booth.admin.get("/api/admin/dashboard"))
    assert dashboard["counters"]["premium_reserved"] == 1


def test_T08_cap_reopen_claim_once_and_mychew_releases_reservation(booth):
    booth.configure(premium=1)
    booth.open(0)
    guest = booth.guest()
    first = ok(booth.submit(guest, WIN))
    error(booth.submit(booth.guest(), WIN), "CAP_REACHED")
    reward = first["reward"]
    assert reward["choices"] == ["SESAME", "ACE"]
    claim = f'/api/entitlements/{reward["entitlement_id"]}/claim'
    # A top voucher is 마이쮸 plus one premium item, never 마이쮸 alone.
    error(guest.post(claim, json={"choice": "MYCHEW"}), "INVALID_CHOICE")
    ok(guest.post(claim, json={"choice": "ACE"}))
    error(guest.post(claim, json={"choice": "ACE"}), "ALREADY_CLAIMED")
    counters = ok(booth.admin.get("/api/admin/dashboard"))["counters"]
    assert counters == {"mychew_used": 1, "premium_reserved": 1}
    inventory = ok(booth.admin.get("/api/admin/inventory"))["inventory"]
    assert inventory == {"MYCHEW": 449, "SESAME": 48, "ACE": 46}
    ok(booth.admin.post("/api/admin/booth", json={"action": "REOPEN", "reason": "재고 다시 확인"}))
    error(booth.submit(guest, WIN), "LIMIT_REACHED")
    assert ok(booth.submit(booth.guest(), WIN))["attempt_no"] == 1
    audit = ok(booth.admin.get("/api/admin/audit"))["items"]
    assert any("재고 다시 확인" in str(a) for a in audit)


def test_T10_freeze_immutable_while_pool_grows_next_day(booth):
    booth.open()
    guest = booth.guest()
    ok(booth.submit(guest))
    board = ok(booth.admin.get("/api/admin/scoreboard"))
    assert board["pool_size"] == 51
    assert len(board["rankings"]) == 1
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    frozen = ok(booth.admin.post("/api/admin/freeze", json={"scope": "DAY1"}))
    assert frozen["frozen"]
    booth.clock[0] += timedelta(days=1)
    ok(booth.admin.post("/api/admin/login", json={"pin": PIN}))
    ok(booth.admin.post("/api/admin/day", json={"day_no": 2}))
    booth.open()
    ok(booth.submit(booth.guest(), WIN))
    assert ok(guest.get("/api/hall?scope=DAY1")) == frozen


def test_T09_cycle_is_held_at_least_sixty_seconds(booth):
    booth.open()
    cycle_strategies = [[26, 36, 15, 7, 17], [40, 13, 13, 30, 5], [46, 11, 0, 14, 30]]
    for i, strategy in enumerate(cycle_strategies):
        ok(booth.submit(booth.guest(f"순환{i}"), strategy))
    first = ok(booth.admin.get("/api/admin/scoreboard"))["cycle"]
    assert first is not None
    original = {r["submission_id"] for r in first}
    booth.clock[0] += timedelta(seconds=1)
    newcomer = booth.guest("새순환")
    strategy = [22, 21, 23, 3, 32]
    ok(booth.submit(newcomer, strategy))
    held = ok(booth.admin.get("/api/admin/scoreboard"))["cycle"]
    assert {r["submission_id"] for r in held} == original
    booth.clock[0] += timedelta(seconds=61)
    ok(booth.submit(booth.guest("새순환둘"), strategy))
    refreshed = ok(booth.admin.get("/api/admin/scoreboard"))["cycle"]
    assert {r["submission_id"] for r in refreshed} != original


def test_T11_idempotency_same_response_after_claim_and_concurrent_retries(booth):
    booth.open()
    guest = booth.guest()
    request_id = uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: booth.submit(guest, TIE, request_id), range(2)))
    first, second = [ok(r) for r in responses]
    assert first == second
    assert len(ok(guest.get("/api/me/submissions"))["items"]) == 1
    reward = first["reward"]
    ok(guest.post(f'/api/entitlements/{reward["entitlement_id"]}/claim', json={"choice": "MYCHEW"}))
    assert ok(booth.submit(guest, TIE, request_id)) == first
    assert ok(guest.get("/api/me/submissions"))["items"][0]["reward"]["status"] == "CLAIMED"


def test_T12_event_stream_snapshot_update_and_polling_api(booth):
    """Exercise the actual SSE producer; live transport/device endurance is separate."""
    booth.open()
    guest = booth.guest()
    endpoint = next(route.endpoint for route in booth.app.routes if getattr(route, "path", None) == "/api/events")

    async def exercise():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        cookie = "; ".join(f"{k}={v}" for k, v in guest.cookies.items())
        request = Request({"type": "http", "method": "GET", "path": "/api/events", "headers": [(b"cookie", cookie.encode())], "scheme": "http", "server": ("testserver", 80), "client": ("testclient", 50000), "query_string": b""}, receive)
        response = await endpoint(request)
        stream = response.body_iterator
        try:
            assert "retry:" in await anext(stream)
            assert "event: state.changed" in await anext(stream)
            first = await anext(stream)
            assert "event: board.updated" in first
            ok(booth.submit(guest))
            start = time.perf_counter()
            for _ in range(5):
                event = await anext(stream)
                if "event: board.updated" in event:
                    elapsed = time.perf_counter() - start
                    data = json.loads(event.split("data: ", 1)[1])
                    assert data["pool_size"] == 51
                    assert elapsed < 2.5  # 2-second cadence, allowing test scheduling.
                    print(f"SSE producer observed committed update after {elapsed:.3f}s; transport endurance untested")
                    break
            else:
                pytest.fail("SSE did not publish the committed change")
        finally:
            await stream.aclose()

    asyncio.run(exercise())
    assert ok(guest.get("/api/scoreboard"))["pool_size"] == 51


def test_T14_sqlite_consistent_backup_restores_counts(booth, tmp_path):
    if booth.postgres:
        pytest.skip("SQLite online backup is local-only; PostgreSQL backup is tested separately with pg_dump")
    booth.open()
    guest = booth.guest()
    ok(booth.submit(guest))
    backup = tmp_path / "checkpoint.db"
    with sqlite3.connect(booth.path) as source, sqlite3.connect(backup) as dest:
        source.backup(dest)
    before = ok(booth.admin.get("/api/admin/dashboard"))
    ok(booth.submit(booth.guest(), WIN))
    with sqlite3.connect(backup) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT COUNT(*) FROM submissions WHERE is_seed=0").fetchone()[0] == before["submission_count"]
        assert restored.execute("SELECT COUNT(*) FROM entitlements WHERE status!='VOID'").fetchone()[0] == before["counters"]["mychew_used"]


def test_T15_student_multidevice_real_name_and_consent(booth):
    booth.open()
    a, response = booth.student(nickname="학생별명")
    identity = ok(response)
    ok(booth.submit(a))
    b, response = booth.student(nickname="바꿀수없음")
    second = ok(response)
    assert second["participant_id"] == identity["participant_id"]
    assert second["nickname"] == identity["nickname"]
    assert second["attempts_left"] == 0
    error(booth.submit(b), "LIMIT_REACHED")
    # the same 학번 with a different name never takes over the record
    _, response = booth.student(name="다른사람이름", nickname="가로채기")
    error(response, "STUDENT_ID_CONFLICT")
    other = booth.guest("외부별명")
    client = TestClient(booth.app)
    error(client.post("/api/session", json={"kind": "GUEST", "nickname": "외부별명", "consent": True}), "NICKNAME_TAKEN")
    response = client.post("/api/session", json={"kind": "GUEST", "nickname": "미동의자", "consent": False})
    assert response.status_code == 400
    assert identity["nickname"] == "학생별명"
    assert "테스트학생" not in str(identity)
    assert "00-000" not in str(identity) and "ksa.hs.kr" not in str(identity)


def test_T16_top10_student_names_without_email_and_purge(booth):
    booth.open()
    students = []
    for i in range(12):
        client, response = booth.student(name="학교실명" + "가나다라마바사아자차카타"[i], student_id=f"26-{i:03d}", nickname=f"학생별명{i}")
        ok(response)
        students.append(client)
        ok(booth.submit(client))
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    hall = ok(booth.admin.post("/api/admin/freeze", json={"scope": "DAY1"}))
    assert [x["rank"] for x in hall["rankings"]] == list(range(1, 11))
    # Winners are named, but their strategies stay secret.
    assert all(set(x) == {"rank", "nickname", "win_rate"} for x in hall["rankings"])
    assert "학생별명" in json.dumps(hall, ensure_ascii=False)
    assert "학교실명" not in json.dumps(hall, ensure_ascii=False)
    assert all(f'"26-{i:03d}"' not in json.dumps(hall, ensure_ascii=False) for i in range(12))
    assert "ksa.hs.kr" not in json.dumps(hall, ensure_ascii=False)
    ok(booth.admin.post("/api/admin/purge", json={"confirm": "PURGE", "reason": "테스트 행사 종료"}))
    public = ok(students[0].get("/api/hall?scope=DAY1"))
    assert "학교실명" not in json.dumps(public, ensure_ascii=False)
    assert "학생별명" not in json.dumps(public, ensure_ascii=False)
    assert ok(students[0].get("/api/state"))["participant_id"] is None
    from server import db
    with db.transaction() as connection:
        assert connection.scalar("SELECT COUNT(*) FROM participants WHERE student_id IS NOT NULL OR real_name IS NOT NULL") == 0
        assert connection.scalar("SELECT COUNT(*) FROM submissions") == 62


def test_security_origin_foreign_request_and_foreign_claim(booth):
    booth.open()
    a, b = booth.guest("참가자하나"), booth.guest("참가자둘")
    request_id = uuid.uuid4().hex
    first = ok(booth.submit(a, TIE, request_id))
    response = booth.submit(b, TIE, request_id)
    assert response.status_code in (403, 409)
    assert response.json().get("id") is None
    entitlement = first["reward"]["entitlement_id"]
    assert b.post(f"/api/entitlements/{entitlement}/claim", json={"choice": "MYCHEW"}).status_code in (403, 404)
    response = a.post("/api/submissions", json={"request_id": uuid.uuid4().hex, "strategy": TIE}, headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403


def test_security_cloud_missing_database_fails_closed(monkeypatch, tmp_path):
    from server import db
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "must-not-create.db"))
    with pytest.raises((RuntimeError, ValueError)):
        db.target()
    assert not (tmp_path / "must-not-create.db").exists()


def test_admin_login_five_failed_attempts_persist(booth):
    for _ in range(5):
        error(booth.admin.post("/api/admin/login", json={"pin": "incorrect-test-pin"}), "UNAUTHORIZED")
    error(booth.admin.post("/api/admin/login", json={"pin": "incorrect-test-pin"}), "RATE_LIMITED")
    error(booth.admin.post("/api/admin/login", json={"pin": PIN}), "RATE_LIMITED")


def test_day_change_rejects_old_unsaved_request_but_replays_saved_request(booth):
    booth.open()
    guest, response = booth.student()
    ok(response)
    body = {"request_id": uuid.uuid4().hex, "strategy": TIE, "expected_day_no": 1}
    first = ok(guest.post("/api/submissions", json=body))
    booth.next_day()
    booth.open()
    assert ok(guest.post("/api/submissions", json=body)) == first
    body["request_id"] = uuid.uuid4().hex
    error(guest.post("/api/submissions", json=body), "DAY_CHANGED")
    assert ok(guest.get("/api/state"))["attempts_left"] == 1


def test_reward_choice_persists_before_claim_and_is_idempotent(booth):
    booth.open(0)
    guest = booth.guest()
    reward = ok(booth.submit(guest, WIN))["reward"]
    eid = reward["entitlement_id"]
    chosen = ok(guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "ACE"}))
    assert chosen["choice"] == "ACE" and chosen["status"] == "ISSUED"
    counters = ok(booth.admin.get("/api/admin/dashboard"))["counters"]
    assert counters == {"mychew_used": 1, "premium_reserved": 1}
    assert ok(guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "ACE"})) == chosen
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == counters
    assert ok(guest.get("/api/me/submissions"))["items"][0]["reward"]["choice"] == "ACE"
    claims = ok(booth.admin.get("/api/admin/claims"))["items"]
    assert next(r for r in claims if r["id"] == eid)["choice"] == "ACE"
    stock = ok(booth.admin.get("/api/admin/inventory"))["inventory"]
    assert stock["MYCHEW"] == 450 and stock["ACE"] == 47  # choosing reserves; physical stock changes on claim
    ok(guest.post(f"/api/entitlements/{eid}/claim", json={"choice": "ACE"}))
    assert ok(booth.admin.get("/api/admin/inventory"))["inventory"] == {"MYCHEW": 449, "SESAME": 48, "ACE": 46}
    assert ok(booth.admin.get("/api/admin/claims"))["by_item"] == {"MYCHEW": 1, "SESAME": 0, "ACE": 1}
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == counters
    assert guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "SESAME"}).status_code == 409


def test_top_voucher_uses_a_mychew_too_so_the_mychew_cap_binds(booth):
    booth.configure(premium=2, mychew=1, rule="PREMIUM_SOFT")
    booth.open(0)
    a = booth.guest("선택하나")
    ea = ok(booth.submit(a, WIN))["reward"]["entitlement_id"]
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == {"mychew_used": 1, "premium_reserved": 1}
    # Premium is left, but the next top player would need a 마이쮸 as well.
    error(booth.submit(booth.guest("선택둘"), WIN), "CAP_REACHED")
    error(a.post(f"/api/entitlements/{ea}/choice", json={"choice": "MYCHEW"}), "INVALID_CHOICE")
    ok(a.post(f"/api/entitlements/{ea}/choice", json={"choice": "ACE"}))
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == {"mychew_used": 1, "premium_reserved": 1}


def test_individual_premium_reservations_are_atomic_and_can_be_changed(booth):
    for item in ("SESAME", "ACE"):
        ok(booth.admin.post("/api/admin/inventory/count", json={"item": item, "count": 1, "reason": "품목별 동시 예약 테스트"}))
    booth.configure(premium=2, rule="PREMIUM_SOFT")
    booth.open(0)
    clients = [booth.guest("품목하나"), booth.guest("품목둘")]
    entitlements = [ok(booth.submit(c, WIN))["reward"]["entitlement_id"] for c in clients]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda i: clients[i].post(f"/api/entitlements/{entitlements[i]}/choice", json={"choice": "SESAME"}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(i for i, r in enumerate(responses) if r.status_code == 200)
    loser = 1 - winner
    error(responses[loser], "OUT_OF_STOCK_CHOICE")
    ok(clients[winner].post(f"/api/entitlements/{entitlements[winner]}/choice", json={"choice": "ACE"}))
    ok(clients[loser].post(f"/api/entitlements/{entitlements[loser]}/choice", json={"choice": "SESAME"}))
    for i, choice in ((winner, "ACE"), (loser, "SESAME")):
        ok(clients[i].post(f"/api/entitlements/{entitlements[i]}/claim", json={"choice": choice}))
    inventory = ok(booth.admin.get("/api/admin/inventory"))["inventory"]
    assert inventory["SESAME"] == inventory["ACE"] == 0
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"]["premium_reserved"] == 2


def test_claim_can_change_selection_only_when_new_choice_is_available(booth):
    for item in ("SESAME", "ACE"):
        ok(booth.admin.post("/api/admin/inventory/count", json={"item": item, "count": 1, "reason": "수령 선택 일치 테스트"}))
    booth.configure(premium=2, rule="PREMIUM_SOFT")
    booth.open(0)
    a, b = booth.guest("수령하나"), booth.guest("수령둘")
    ea = ok(booth.submit(a, WIN))["reward"]["entitlement_id"]
    eb = ok(booth.submit(b, WIN))["reward"]["entitlement_id"]
    ok(a.post(f"/api/entitlements/{ea}/choice", json={"choice": "SESAME"}))
    ok(b.post(f"/api/entitlements/{eb}/choice", json={"choice": "ACE"}))
    error(a.post(f"/api/entitlements/{ea}/claim", json={"choice": "ACE"}), "OUT_OF_STOCK_CHOICE")
    assert ok(a.get("/api/me/submissions"))["items"][0]["reward"]["choice"] == "SESAME"
    result = ok(a.post(f"/api/entitlements/{ea}/claim", json={"choice": "SESAME"}))
    assert result["choice"] == "SESAME"
    inventory = ok(booth.admin.get("/api/admin/inventory"))["inventory"]
    assert inventory["SESAME"] == 0 and inventory["ACE"] == 1


def test_30_concurrent_submissions_1700_pool(booth):
    """50 seeds + 550 ranked people × 3 attempts, then 30 simultaneous API submits.

    Synthetic historical submissions are inserted directly into this disposable
    DB, with a full exact score cache. Historical reward issuance is omitted so
    the benchmark isolates the 30 new reservations. No production DB is touched.
    """
    from server import db, service, engine
    rng = random.Random(307)
    with db.transaction(write=True) as connection:
        for person_index in range(550):
            pid = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO participants(id,kind,nickname,consent_at) VALUES(?,?,?,?)",
                (pid, "GUEST", f"기존{person_index:03d}", service.stamp()),
            )
            for attempt in (1, 2, 3):
                cuts = sorted(rng.sample(range(105), 4))
                previous, strategy = -1, []
                for cut in cuts + [105]:
                    strategy.append(cut - previous - 1)
                    previous = cut
                submitted_at = (booth.clock[0] - timedelta(minutes=20) + timedelta(microseconds=person_index * 3 + attempt)).isoformat()
                connection.execute(
                    "INSERT INTO submissions(id,request_id,participant_id,day_no,attempt_no,l1,l2,l3,l4,l5,is_seed,is_ranked,submitted_at) VALUES(?,?,?,1,?,?,?,?,?,?,0,1,?)",
                    (uuid.uuid4().hex, uuid.uuid4().hex, pid, attempt, *strategy, submitted_at),
                )
        rows = service.pool_rows(connection)
        assert len(rows) == 1700
        analysis = engine.analyze_pool(rows)
        db.set_config(connection, "score_cache", service.dumps(analysis))
        for row in rows:
            value = analysis[row["id"]]
            connection.execute("UPDATE submissions SET points=?,opponents=? WHERE id=?", (value["points"], value["opponents"], row["id"]))
        db.bump(connection)
    booth.open(10000)
    clients = [booth.guest(f"동시{i:02d}") for i in range(30)]
    barrier = threading.Barrier(30)

    def submit(client):
        barrier.wait()
        begin = time.perf_counter()
        response = booth.submit(client)
        return response, (time.perf_counter() - begin) * 1000

    with ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(submit, clients))
    for response, _ in results:
        assert ok(response)["attempt_no"] == 1
    dashboard = ok(booth.admin.get("/api/admin/dashboard"))
    assert dashboard["counters"]["mychew_used"] == 30
    assert dashboard["counters"]["premium_reserved"] == 0
    assert dashboard["pool_size"] == 1730
    with db.transaction() as connection:
        rows = service.pool_rows(connection)
        cached = json.loads(db.config(connection, "score_cache"))
        fresh = engine.analyze_pool(rows)
    assert len(engine.rank_rows(rows, analysis=fresh)) == 580
    assert {key: (value["points"], value["opponents"]) for key, value in cached.items()} == {key: (value["points"], value["opponents"]) for key, value in fresh.items()}
    latencies = sorted(ms for _, ms in results)
    label = "PostgreSQL" if booth.postgres else "SQLite"
    print(f"30 simultaneous API submissions, initial pool 1700 (50 seeds + 550 ranked people x3): p95={latencies[28]:.1f} ms, max={latencies[-1]:.1f} ms ({label}/TestClient), errors=0; all1730 cached point totals match full recomputation")


TEST_DAY = 9


def test_test_mode_is_isolated_and_leaves_real_days_unlocked(booth):
    admin = booth.admin
    ok(admin.post("/api/admin/day", json={"day_no": TEST_DAY}))
    booth.tries(3)
    # Outside every real date and session: test mode ignores the schedule.
    booth.clock[0] = datetime(2026, 10, 27, 22, 0, tzinfo=KST)
    booth.open(threshold=0)
    guest = booth.guest("시험손님")
    assert ok(guest.get("/api/state"))["test_mode"] is True
    first = ok(booth.submit(guest, WIN))
    assert first["opponents"] == 50 and first["reward"]["kind"] == "TOP_CHOICE"
    stock = ok(admin.get("/api/admin/inventory"))["inventory"]
    ok(guest.post(f"/api/entitlements/{first['reward']['entitlement_id']}/claim", json={"choice": "ACE"}))
    assert ok(admin.get("/api/admin/inventory"))["inventory"] == stock
    ok(booth.submit(guest))
    ok(booth.submit(guest))
    error(booth.submit(guest), "LIMIT_REACHED")
    # The test threshold stays editable while the test booth is open.
    ok(admin.post("/api/admin/threshold", json={"value_bp": 7200, "source": "MANUAL", "reason": "테스트 재설정"}))
    dashboard = ok(admin.get("/api/admin/dashboard"))
    assert dashboard["test_mode"] and dashboard["counters"]["premium_reserved"] == 1
    assert dashboard["test_submissions"] == 3 and not dashboard["threshold_locked"]

    ok(admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    assert ok(admin.post("/api/admin/test/clear", json={}))["test_submissions"] == 0
    assert ok(guest.get("/api/state"))["participant_id"] is None
    ok(admin.post("/api/admin/day", json={"day_no": 1}))
    dashboard = ok(admin.get("/api/admin/dashboard"))
    assert dashboard["counters"] == {"mychew_used": 0, "premium_reserved": 0}
    assert dashboard["pool_size"] == 50 and not dashboard["threshold_locked"]
    booth.configure()
    ok(admin.post("/api/admin/seeds", json={"strategy": TIE, "tag": "추가", "author": "부원"}))
    booth.clock[0] = datetime(2026, 10, 30, 10, 30, tzinfo=KST)
    booth.open()
    assert ok(booth.submit(booth.guest("실제손님")))["opponents"] == 51


def test_test_mode_between_real_days_keeps_pools_and_day_order(booth):
    admin = booth.admin
    booth.open()
    ok(booth.submit(booth.guest("일차손님"), WIN))
    error(admin.post("/api/admin/day", json={"day_no": TEST_DAY}), "DAY_BUSY")
    ok(admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    ok(admin.post("/api/admin/day", json={"day_no": TEST_DAY}))
    booth.open(threshold=0)
    result = ok(booth.submit(booth.guest("시험둘")))
    assert result["opponents"] == 50 and result["pool_size"] == 51
    board = ok(booth.admin.get("/api/admin/scoreboard"))
    assert board["test_mode"] and [r["nickname"] for r in board["rankings"]] == ["시험둘"]
    ok(admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    ok(admin.post("/api/admin/day", json={"day_no": 1}))
    assert [r["nickname"] for r in ok(booth.admin.get("/api/admin/scoreboard"))["rankings"]] == ["일차손님"]
    ok(admin.post("/api/admin/day", json={"day_no": 2}))
    ok(admin.post("/api/admin/day", json={"day_no": TEST_DAY}))
    # Visiting test mode is not a way back to an earlier real day.
    error(admin.post("/api/admin/day", json={"day_no": 1}), "INVALID_DAY")
    ok(admin.post("/api/admin/day", json={"day_no": 2}))


def test_test_mode_opens_and_scores_without_seeds(booth):
    admin = booth.admin
    ok(admin.post("/api/admin/reset", json={"confirm": "RESET", "reason": "시드 없는 테스트"}))
    # Real days still require 50 seeds.
    ok(admin.post("/api/admin/threshold", json={"value_bp": 0, "source": "MANUAL", "reason": "테스트"}))
    error(admin.post("/api/admin/booth", json={"action": "OPEN"}), "POOL_TOO_SMALL")
    ok(admin.post("/api/admin/day", json={"day_no": TEST_DAY}))
    booth.open(threshold=0)
    first = ok(booth.submit(booth.guest("첫시험")))
    # No opponents yet: defined win rate, and nobody qualifies against an empty pool.
    assert first["opponents"] == 0 and first["win_rate"] == 0.0 and first["reward"]["kind"] == "BASIC"
    second = ok(booth.submit(booth.guest("둘째시험"), WIN))
    assert second["opponents"] == 1 and second["reward"]["kind"] == "TOP_CHOICE"
    board = ok(booth.admin.get("/api/admin/scoreboard"))
    assert board["test_mode"] and len(board["rankings"]) == 2


def test_admin_submission_log_delete_returns_try_and_renumbers(booth):
    admin = booth.admin
    booth.tries(3)
    booth.open()
    guest = booth.guest("기록손님")
    first = ok(booth.submit(guest, WIN))
    ok(booth.submit(guest, TIE))
    ok(booth.submit(guest, LOSE))
    log = ok(admin.get("/api/admin/submissions"))
    assert log["day_no"] == 1 and [r["attempt_no"] for r in log["items"]] == [3, 2, 1]
    oldest = log["items"][-1]
    assert oldest["nickname"] == "기록손님" and oldest["kind"] == "GUEST" and oldest["strategy"] == WIN
    assert oldest["entitlement"]["code"] == first["reward"]["code"] and oldest["entitlement"]["status"] == "ISSUED"
    assert oldest["top_bp"] == first["top_bp"]
    assert all(r["entitlement"] is None for r in log["items"][:2])
    error(admin.post(f"/api/admin/submissions/{oldest['id']}/delete", json={}), "REASON_REQUIRED")
    ok(admin.post(f"/api/admin/submissions/{oldest['id']}/delete", json={"reason": "잘못 입력"}))
    left = ok(admin.get("/api/admin/submissions?day=1"))["items"]
    assert [(r["attempt_no"], r["strategy"]) for r in left] == [(2, LOSE), (1, TIE)]
    assert ok(guest.get("/api/state"))["attempts_left"] == 1
    assert ok(admin.get("/api/admin/dashboard"))["counters"]["mychew_used"] == 0
    again = ok(booth.submit(guest, WIN))
    assert again["attempt_no"] == 3 and again["reward"] is None
    assert sorted(r["attempt_no"] for r in ok(guest.get("/api/me/submissions"))["items"]) == [1, 2, 3]
    assert len(ok(admin.get("/api/admin/submissions?day=ALL"))["items"]) == 3
    assert ok(admin.get("/api/admin/submissions?day=2"))["items"] == []
    error(admin.get("/api/admin/submissions?day=5"), "INVALID_DAY")
    actions = [r["action"] for r in ok(admin.get("/api/admin/audit"))["items"]]
    assert "SUBMISSION_DELETED" in actions


def test_admin_snack_adjustments_keep_stock_ledger_true(booth):
    admin = booth.admin
    booth.tries(2)
    booth.open(0)
    guest = booth.guest("간식손님")
    first = ok(booth.submit(guest, WIN))
    second = ok(booth.submit(guest, TIE))
    stock = lambda: ok(admin.get("/api/admin/inventory"))["inventory"]
    start = stock()
    snack = lambda sid, **body: admin.post(f"/api/admin/submissions/{sid}/snack", json={"reason": "현장 정정", **body})
    entry = lambda sid: next(r for r in ok(admin.get("/api/admin/submissions"))["items"] if r["id"] == sid)["entitlement"]

    # A top voucher hands out a 마이쮸 and the premium item.
    ok(snack(first["id"], kind="TOP_CHOICE", choice="SESAME", status="CLAIMED"))
    assert stock() == {**start, "MYCHEW": start["MYCHEW"] - 1, "SESAME": start["SESAME"] - 1} and entry(first["id"])["claimed_at"]
    # Correcting the item handed out moves the stock between items; the 마이쮸 stays out.
    ok(snack(first["id"], kind="TOP_CHOICE", choice="ACE", status="CLAIMED"))
    assert stock() == {**start, "MYCHEW": start["MYCHEW"] - 1, "ACE": start["ACE"] - 1}
    # Downgrading a claimed top voucher to 마이쮸 returns only the premium item.
    ok(snack(first["id"], kind="BASIC", status="CLAIMED"))
    assert stock() == {**start, "MYCHEW": start["MYCHEW"] - 1}
    ok(snack(first["id"], kind="TOP_CHOICE", choice="ACE", status="CLAIMED"))
    # Back to unclaimed: the items stay out unless they were physically returned.
    ok(snack(first["id"], kind="TOP_CHOICE", choice="ACE", status="ISSUED"))
    assert stock()["ACE"] == start["ACE"] - 1 and entry(first["id"])["claimed_at"] is None
    ok(snack(first["id"], kind="TOP_CHOICE", choice="ACE", status="CLAIMED"))
    ok(snack(first["id"], kind=None, return_stock=True))
    assert stock() == {**start, "MYCHEW": start["MYCHEW"] - 1, "ACE": start["ACE"] - 1} and entry(first["id"]) is None
    error(snack(first["id"], kind="TOP_CHOICE", status="CLAIMED"), "INVALID_CHOICE")
    error(snack(first["id"], kind="TOP_CHOICE", choice="MYCHEW", status="ISSUED"), "INVALID_CHOICE")
    error(snack(first["id"], kind="BASIC", choice="ACE", status="ISSUED"), "INVALID_CHOICE")
    # A voucher can be issued on any play, here the second try.
    ok(snack(second["id"], kind="BASIC", status="ISSUED"))
    created = entry(second["id"])
    assert created["kind"] == "BASIC" and created["status"] == "ISSUED" and len(created["code"]) == 6
    assert ok(admin.get("/api/admin/dashboard"))["counters"] == {"mychew_used": 1, "premium_reserved": 0}
    ok(guest.post(f"/api/entitlements/{created['id']}/claim", json={"choice": "MYCHEW"}))
    ok(admin.post(f"/api/admin/submissions/{second['id']}/delete", json={"reason": "중복", "return_stock": True}))
    assert stock()["MYCHEW"] == start["MYCHEW"] - 1
    assert ok(admin.get("/api/admin/dashboard"))["counters"]["mychew_used"] == 0
    assert any(r["reason"] == "ADMIN_ADJUST" for r in ok(admin.get("/api/admin/inventory"))["ledger"])


def test_live_top_share_rule_is_default_and_judges_against_whole_pool(booth):
    admin = booth.admin
    # No mode given: the live top-share rule applies (15% here).
    assert ok(admin.post("/api/admin/threshold", json={"value_bp": 1500, "reason": "실시간 상위 15%"}))["mode"] == "LIVE"
    ok(admin.post("/api/admin/booth", json={"action": "OPEN"}))
    assert ok(admin.get("/api/admin/dashboard"))["pass_mode"] == "LIVE"
    # Against 50 TIE seeds, WIN beats them all (1st of 51 -> top 1.97%), LOSE loses to all.
    winner = ok(booth.submit(booth.guest("상위손님"), WIN))
    assert winner["pass_mode"] == "LIVE" and winner["top_bp"] == 197
    assert winner["reward"]["kind"] == "TOP_CHOICE"
    loser = ok(booth.submit(booth.guest("하위손님"), LOSE))
    assert loser["top_bp"] == 10000 and loser["reward"]["kind"] == "BASIC"


def test_live_rule_boundary_and_mode_validation(booth):
    admin = booth.admin
    error(admin.post("/api/admin/threshold", json={"mode": "PERCENT", "value_bp": 1500, "reason": "잘못된 방식"}), "INVALID_INPUT")
    # 1st of 51 is 197/10000 (rounded up); a 1.96% cutoff misses it, 1.97% would pass.
    ok(admin.post("/api/admin/threshold", json={"mode": "LIVE", "value_bp": 196, "reason": "경계"}))
    ok(admin.post("/api/admin/booth", json={"action": "OPEN"}))
    first = ok(booth.submit(booth.guest("경계손님"), WIN))
    assert first["top_bp"] == 197 and first["reward"]["kind"] == "BASIC"


def test_threshold_mode_column_is_added_to_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE threshold_history (id TEXT PRIMARY KEY, day_no INTEGER NOT NULL, value_bp INTEGER NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL)")
        old.execute("INSERT INTO threshold_history VALUES('t1',1,7200,'MANUAL','이전 값','ADMIN','2026-10-29T09:00:00+09:00')")
        old.execute("CREATE TABLE submissions (id TEXT PRIMARY KEY, participant_id TEXT, day_no INTEGER NOT NULL)")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("DB_PATH", str(path))
    from server import db
    db.initialize(force=True)
    db.initialize(force=True)  # idempotent
    with sqlite3.connect(path) as new:
        assert new.execute("SELECT mode,value_bp FROM threshold_history").fetchall() == [("FIXED", 7200)]
        assert "top_bp" in [row[1] for row in new.execute("PRAGMA table_info(submissions)")]


def test_public_board_hides_strategies_and_result_shows_place_in_pool(booth):
    booth.open()
    winner = ok(booth.submit(booth.guest("숨김일등"), WIN))
    loser = ok(booth.submit(booth.guest("숨김꼴찌"), LOSE))
    # The place and share use the same pool as the snack rule (50 seeds + both plays).
    assert winner["rank_estimate"]["rank"] == 1 and winner["rank_estimate"]["of"] == 51
    assert loser["rank_estimate"]["rank"] == 52 and loser["rank_estimate"]["of"] == 52 and loser["top_bp"] == 10000
    assert winner["today_count"] == 1 and loser["today_count"] == 2
    public = ok(booth.client().get("/api/scoreboard"))
    assert public["today_count"] == 2 and public["pool_size"] == 52
    assert "rankings" not in public and "cycle" not in public
    assert all(set(r) == {"nickname", "submitted_at"} for r in public["recent"])
    assert sorted(r["nickname"] for r in public["recent"]) == ["숨김꼴찌", "숨김일등"]
    error(booth.client().get("/api/admin/scoreboard"), "UNAUTHORIZED")
    full = ok(booth.admin.get("/api/admin/scoreboard"))
    assert full["rankings"][0]["nickname"] == "숨김일등" and full["rankings"][0]["strategy"] == WIN


def test_live_board_shows_ten_plays_just_under_the_cut_not_the_winners(booth):
    from server import db, service
    booth.open(1200, mode="LIVE")
    rng = random.Random(42)
    for i in range(30):
        cuts = sorted(rng.sample(range(102), 4))
        strategy = [b - a for a, b in zip([0] + cuts, cuts + [101])]
        ok(booth.submit(booth.guest(f"사다리{i:02d}"), strategy))
    near = ok(booth.client().get("/api/scoreboard"))["near_cut"]
    assert near["mode"] == "LIVE" and near["cut_bp"] == 1200 and near["target_bp"] == 1500
    with db.transaction() as c:
        rows = service.pool_rows(c)
        analysis = service.analysis_in(c, rows)
    share = sorted(service.top_share_bp(rows, analysis, r["id"]) for r in rows)
    samples = near["samples"]
    assert len(samples) == 10 and all(x["top_bp"] > 1200 for x in samples)
    assert [x["top_bp"] for x in samples] == sorted(x["top_bp"] for x in samples)
    # Nobody under the cut who is left out sits closer to 15% than the farthest sample shown.
    farthest = max(abs(x["top_bp"] - 1500) for x in samples)
    left_out = list(share)
    for x in samples: left_out.remove(x["top_bp"])
    assert all(abs(v - 1500) >= farthest for v in left_out if v > 1200)
    assert any(v <= 1200 for v in share)  # winners exist and are never shown
