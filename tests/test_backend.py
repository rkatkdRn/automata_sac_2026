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
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
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
    from server import service, google_auth
    from server.app import create_app
    clock = [datetime(2026, 10, 30, 10, 30, tzinfo=KST)]
    monkeypatch.setattr(service, "now", lambda: clock[0])
    verified_credentials = {}
    # Game-flow tests trust only this boundary. test_school_auth.py separately
    # exercises Google's real cryptographic verifier with locally signed JWTs.
    def verified_claims(credential, audience):
        assert audience == "test-client.apps.googleusercontent.com"
        return dict(verified_credentials[credential])
    monkeypatch.setattr(google_auth, "verify_credential", verified_claims)
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
            self.credentials = verified_credentials

        def client(self):
            return stack.enter_context(TestClient(app))

        def google_token(self, client, *, name="테스트학생", student_id="23-103", sub=None, claims=None):
            nonce = ok(client.post("/api/auth/google/challenge", json={"consent": True}))["nonce"]
            token = "test-token-" + uuid.uuid4().hex
            verified_credentials[token] = {
                "aud": "test-client.apps.googleusercontent.com", "iss": "https://accounts.google.com",
                "exp": int(time.time()) + 600, "iat": int(time.time()),
                "sub": sub or "google-sub-" + student_id, "hd": "ksa.hs.kr",
                "email": student_id + "@ksa.hs.kr", "email_verified": True,
                "name": name, "nonce": nonce,
            }
            if claims: verified_credentials[token].update(claims)
            return token

        def guest(self, nickname=None):
            client = stack.enter_context(TestClient(app))
            ok(client.post("/api/session", json={"kind": "GUEST", "nickname": nickname or "손님" + uuid.uuid4().hex[:5], "consent": True}))
            return client

        def student(self, name="테스트학생", student_id="23-103", nickname="학생별명"):
            client = stack.enter_context(TestClient(app))
            credential = self.google_token(client, name=name, student_id=student_id)
            response = client.post("/api/auth/google", json={"credential": credential, "nickname": nickname, "consent": True})
            return client, response

        def configure(self, *, day=1, premium=40, mychew=188, rule="EITHER"):
            return ok(admin.post("/api/admin/day-config", json={
                "day_no": day, "date": f"2026-{10 if day == 1 else 10 if day == 2 else 11:02d}-{30 if day == 1 else 31 if day == 2 else 1:02d}",
                "sessions": [["10:00", "13:00"], ["14:00", "17:00"]],
                "mychew_cap_cum": mychew, "premium_cap_cum": premium, "close_rule": rule, "reason": "테스트 설정",
            }))

        def open(self, threshold=7200):
            ok(admin.post("/api/admin/threshold", json={"value_bp": threshold, "source": "MANUAL", "reason": "테스트 임계값"}))
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
    assert ok(guest.get("/api/state"))["attempts_left"] == 3
    assert ok(guest.get("/api/me/submissions"))["items"] == []


def test_T04_T05_T13_three_attempts_next_day_and_self_exclusion(booth):
    booth.open()
    # Students retain their identity across days; guests now expire after 12h.
    guest, response = booth.student()
    ok(response)
    first = ok(booth.submit(guest))
    assert first["attempt_no"] == 1 and first["reward"]["kind"] == "BASIC"
    second = ok(booth.submit(guest, WIN))
    third = ok(booth.submit(guest, LOSE))
    assert second["reward"] is None and third["reward"] is None
    assert second["opponents"] == third["opponents"] == 50
    error(booth.submit(guest), "LIMIT_REACHED")
    booth.next_day()
    booth.open()
    again = ok(booth.submit(guest))
    assert again["attempt_no"] == 1 and again["attempts_left"] == 2
    assert again["opponents"] == 50
    assert again["pool_size"] >= 53


def test_T06_threshold_locks_after_any_open_even_close(booth):
    booth.open()
    body = {"value_bp": 7100, "source": "MANUAL", "reason": "다시 변경 시도"}
    error(booth.admin.post("/api/admin/threshold", json=body), "THRESHOLD_LOCKED")
    ok(booth.admin.post("/api/admin/booth", json={"action": "PAUSE"}))
    error(booth.admin.post("/api/admin/threshold", json=body), "THRESHOLD_LOCKED")
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    error(booth.admin.post("/api/admin/threshold", json=body), "THRESHOLD_LOCKED")


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
    error(booth.submit(guest, WIN), "CAP_REACHED")
    reward = first["reward"]
    claim = f'/api/entitlements/{reward["entitlement_id"]}/claim'
    ok(guest.post(claim, json={"choice": "MYCHEW"}))
    error(guest.post(claim, json={"choice": "MYCHEW"}), "ALREADY_CLAIMED")
    counters = ok(booth.admin.get("/api/admin/dashboard"))["counters"]
    assert counters == {"mychew_used": 1, "premium_reserved": 0}
    ok(booth.admin.post("/api/admin/booth", json={"action": "REOPEN", "reason": "재고 다시 확인"}))
    assert ok(booth.submit(guest, WIN))["attempt_no"] == 2
    audit = ok(booth.admin.get("/api/admin/audit"))["items"]
    assert any("재고 다시 확인" in str(a) for a in audit)


def test_T10_freeze_immutable_while_pool_grows_next_day(booth):
    booth.open()
    guest = booth.guest()
    ok(booth.submit(guest))
    board = ok(guest.get("/api/scoreboard"))
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
    first = ok(booth.admin.get("/api/scoreboard"))["cycle"]
    assert first is not None
    original = {r["submission_id"] for r in first}
    booth.clock[0] += timedelta(seconds=1)
    newcomer = booth.guest("새순환")
    strategy = [22, 21, 23, 3, 32]
    ok(booth.submit(newcomer, strategy))
    held = ok(booth.admin.get("/api/scoreboard"))["cycle"]
    assert {r["submission_id"] for r in held} == original
    booth.clock[0] += timedelta(seconds=61)
    ok(booth.submit(newcomer, strategy))
    refreshed = ok(booth.admin.get("/api/scoreboard"))["cycle"]
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
    ok(booth.submit(guest, WIN))
    with sqlite3.connect(backup) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT COUNT(*) FROM submissions WHERE is_seed=0").fetchone()[0] == before["submission_count"]
        assert restored.execute("SELECT COUNT(*) FROM entitlements WHERE status!='VOID'").fetchone()[0] == before["counters"]["mychew_used"]


def test_T15_student_multidevice_real_name_and_consent(booth):
    booth.open()
    a, response = booth.student()
    identity = ok(response)
    ok(booth.submit(a))
    b, response = booth.student(nickname="바꿀수없음")
    second = ok(response)
    assert second["participant_id"] == identity["participant_id"]
    assert second["nickname"] == identity["nickname"]
    assert second["attempts_left"] == 2
    assert ok(booth.submit(b))["attempt_no"] == 2
    _, response = booth.student(name="학교계정변경이름")
    updated = ok(response)
    assert updated["participant_id"] == identity["participant_id"]
    assert updated["nickname"] == "학교계정변경이름"
    other = booth.guest("외부별명")
    client = TestClient(booth.app)
    error(client.post("/api/session", json={"kind": "GUEST", "nickname": "외부별명", "consent": True}), "NICKNAME_TAKEN")
    response = client.post("/api/session", json={"kind": "GUEST", "nickname": "미동의자", "consent": False})
    assert response.status_code == 400
    assert identity["nickname"] == "테스트학생"
    assert "23-103" not in str(identity) and "ksa.hs.kr" not in str(identity)


def test_T16_top10_student_names_without_email_and_purge(booth):
    booth.open()
    students = []
    for i in range(12):
        client, response = booth.student(name=f"학교실명{i}", student_id=f"26-{i:03d}", nickname=f"학생입력무시{i}")
        ok(response)
        students.append(client)
        ok(booth.submit(client))
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    hall = ok(booth.admin.post("/api/admin/freeze", json={"scope": "DAY1"}))
    assert len(hall["rankings"]) == 10
    assert [x["rank"] for x in hall["rankings"]] == list(range(1, 11))
    assert "학교실명" in json.dumps(hall, ensure_ascii=False)
    assert "학생입력무시" not in json.dumps(hall, ensure_ascii=False)
    assert all(f'"26-{i:03d}"' not in json.dumps(hall, ensure_ascii=False) for i in range(12))
    assert "ksa.hs.kr" not in json.dumps(hall, ensure_ascii=False)
    ok(booth.admin.post("/api/admin/purge", json={"confirm": "PURGE", "reason": "테스트 행사 종료"}))
    public = ok(students[0].get("/api/hall?scope=DAY1"))
    assert "학교실명" not in json.dumps(public, ensure_ascii=False)
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
    assert ok(guest.get("/api/state"))["attempts_left"] == 3


def test_reward_choice_persists_before_claim_and_is_idempotent(booth):
    booth.open(0)
    guest = booth.guest()
    reward = ok(booth.submit(guest, WIN))["reward"]
    eid = reward["entitlement_id"]
    chosen = ok(guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "MYCHEW"}))
    assert chosen["choice"] == "MYCHEW" and chosen["status"] == "ISSUED"
    counters = ok(booth.admin.get("/api/admin/dashboard"))["counters"]
    assert counters == {"mychew_used": 1, "premium_reserved": 0}
    assert ok(guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "MYCHEW"})) == chosen
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == counters
    assert ok(guest.get("/api/me/submissions"))["items"][0]["reward"]["choice"] == "MYCHEW"
    claims = ok(booth.admin.get("/api/admin/claims"))["items"]
    assert next(r for r in claims if r["id"] == eid)["choice"] == "MYCHEW"
    stock = ok(booth.admin.get("/api/admin/inventory"))["inventory"]
    assert stock["MYCHEW"] == 450  # choosing reserves; physical stock changes on claim
    ok(guest.post(f"/api/entitlements/{eid}/claim", json={"choice": "MYCHEW"}))
    assert ok(booth.admin.get("/api/admin/inventory"))["inventory"]["MYCHEW"] == 449
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == counters
    assert guest.post(f"/api/entitlements/{eid}/choice", json={"choice": "ACE"}).status_code == 409


def test_choice_transfer_obeys_cap_and_releases_previous_reservation(booth):
    booth.configure(premium=2, mychew=1, rule="PREMIUM_SOFT")
    booth.open(0)
    a, b = booth.guest("선택하나"), booth.guest("선택둘")
    ea = ok(booth.submit(a, WIN))["reward"]["entitlement_id"]
    eb = ok(booth.submit(b, WIN))["reward"]["entitlement_id"]
    ok(a.post(f"/api/entitlements/{ea}/choice", json={"choice": "MYCHEW"}))
    error(b.post(f"/api/entitlements/{eb}/choice", json={"choice": "MYCHEW"}), "OUT_OF_STOCK_CHOICE")
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == {"mychew_used": 1, "premium_reserved": 1}
    ok(a.post(f"/api/entitlements/{ea}/choice", json={"choice": "ACE"}))
    assert ok(booth.admin.get("/api/admin/dashboard"))["counters"] == {"mychew_used": 0, "premium_reserved": 2}
    ok(b.post(f"/api/entitlements/{eb}/choice", json={"choice": "MYCHEW"}))
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
