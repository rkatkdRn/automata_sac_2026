"""Student (학번·이름·닉네임) and guest sign-in, session isolation and identity privacy.

DBs are disposable; see test_backend.booth. Google sign-in was removed, so earlier Google
sessions only appear here as leftover rehearsal data.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import pytest
from fastapi.testclient import TestClient
from test_backend import booth, ok, error


def join_student(client, **fields):
    body = {"kind": "STUDENT", "student_id": "00-000", "name": "테스트학생", "nickname": "학생별명", "consent": True}
    body.update(fields)
    return client.post("/api/session", json=body)


def legacy_google_student(booth, *, student_id="00-000", name="테스트학생"):
    """A student who signed in through the removed Google flow before choosing a nickname."""
    from server import db, service
    client = booth.client()
    token = "legacy-school-session-" + student_id
    with db.transaction(write=True) as c:
        pid = service.uid()
        c.execute("INSERT INTO participants(id,kind,student_id,real_name,nickname,consent_at) VALUES(?,?,?,?,?,?)",
                  (pid, "STUDENT", student_id, name, "school-" + pid, service.stamp()))
        c.execute("INSERT INTO school_identities(google_sub,participant_id,school_email,verified_at) VALUES(?,?,?,?)",
                  ("google-sub-" + student_id, pid, student_id + "@ksa.hs.kr", service.stamp()))
        c.execute("INSERT INTO sessions(token_hash,participant_id,role,expires_at,created_at) VALUES(?,?,?,?,?)",
                  (service.digest(token), pid, "SCHOOL", (service.now() + timedelta(days=30)).isoformat(), service.stamp()))
    client.cookies.set(service.COOKIE, token)
    return client, pid


def ok_cookie(response):
    ok(response)
    return response.headers["set-cookie"]


def test_google_sign_in_routes_are_gone(booth):
    client = booth.client()
    for path in ("/api/auth/config", "/api/auth/google/challenge", "/api/auth/google"):
        assert client.post(path, json={"consent": True}).status_code in (404, 405)
    assert client.get("/api/auth/config").status_code == 404


def test_student_signs_in_with_id_name_and_nickname(booth):
    booth.open()
    client = booth.client()
    session = ok(join_student(client, student_id="26001", name="홍길동", nickname="번개전술"))
    assert session["kind"] == "STUDENT" and session["auth_method"] == "STUDENT_ID"
    assert session["nickname"] == "번개전술" and session["needs_nickname"] is False
    state = ok(client.get("/api/state"))
    assert state["participant_id"] == session["participant_id"] and state["nickname"] == "번개전술"
    ok(booth.submit(client))
    public = json.dumps([ok(client.get("/api/scoreboard")), state, session], ensure_ascii=False)
    assert "번개전술" in public
    assert "홍길동" not in public and "26-001" not in public
    from server import db
    with db.transaction() as c:
        row = c.one("SELECT student_id,real_name,nickname_chosen FROM participants WHERE id=?", (session["participant_id"],))
    assert row == {"student_id": "26-001", "real_name": "홍길동", "nickname_chosen": 1}


@pytest.mark.parametrize("student_id", ["", "2600", "260011", "26_001", "ab-cde", "26-0011", None, 26001])
def test_student_id_must_be_two_digits_dash_three_digits(booth, student_id):
    error(join_student(booth.client(), student_id=student_id), "INVALID_STUDENT_ID")


@pytest.mark.parametrize("name", ["", "김", "<b>김</b>", "김철수1", "a@b", "가" * 31, None, ["김철수"]])
def test_name_is_validated(booth, name):
    error(join_student(booth.client(), name=name), "INVALID_NAME")


def test_same_id_and_name_continue_on_another_device(booth):
    booth.open()
    a = booth.client()
    first = ok(join_student(a, name="홍길동", nickname="번개전술"))
    ok(booth.submit(a))
    b = booth.client()
    # 학번 without the hyphen and the name with stray spaces still match; the typed nickname is ignored
    second = ok(join_student(b, student_id="00000", name=" 홍 길동 ", nickname="아무거나"))
    assert second["participant_id"] == first["participant_id"]
    assert second["nickname"] == "번개전술" and second["attempts_left"] == 0
    error(booth.submit(b), "LIMIT_REACHED")
    assert ok(a.get("/api/state"))["attempts_left"] == 0


def test_different_name_cannot_take_over_a_student_id(booth):
    owner = booth.client()
    mine = ok(join_student(owner, name="홍길동", nickname="번개전술"))
    intruder = booth.client()
    error(join_student(intruder, name="김철수", nickname="가로채기"), "STUDENT_ID_CONFLICT")
    assert ok(intruder.get("/api/state"))["participant_id"] is None
    assert ok(owner.get("/api/state"))["participant_id"] == mine["participant_id"]
    from server import db
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM participants") == 1


def test_parallel_first_sign_ins_create_one_record(booth):
    clients = [booth.client() for _ in range(6)]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda c: join_student(c, name="홍길동", nickname="번개전술"), clients))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["participant_id"] for r in results}) == 1


def test_nicknames_are_unique_across_students_and_guests(booth):
    booth.guest("손님별명")
    error(join_student(booth.client(), nickname="손님별명"), "NICKNAME_TAKEN")
    ok(join_student(booth.client(), student_id="11-111", name="홍길동", nickname="번개전술"))
    error(join_student(booth.client(), student_id="22-222", name="김철수", nickname="번개전술"), "NICKNAME_TAKEN")
    error(booth.client().post("/api/session", json={"kind": "GUEST", "nickname": "번개전술", "consent": True}), "NICKNAME_TAKEN")
    error(join_student(booth.client(), student_id="33-333", name="이영희", nickname="school-x"), "INVALID_NICKNAME")
    error(join_student(booth.client(), student_id="33-333", name="이영희", nickname="새별명", consent=False), "CONSENT_REQUIRED")


def test_same_name_different_ids_are_separate_players(booth):
    booth.open()
    a, b = booth.client(), booth.client()
    aid = ok(join_student(a, student_id="00-000", name="동명이인", nickname="첫째별명"))["participant_id"]
    bid = ok(join_student(b, student_id="24-103", name="동명이인", nickname="둘째별명"))["participant_id"]
    assert aid != bid
    ok(booth.submit(a))
    ok(booth.submit(b))
    rows = ok(a.get("/api/scoreboard"))["rankings"]
    assert {r["nickname"] for r in rows if r["participant_id"] in (aid, bid)} == {"첫째별명", "둘째별명"}
    assert "동명이인" not in json.dumps(rows, ensure_ascii=False)


def test_student_session_lasts_thirty_days(booth):
    client = booth.client()
    pid = ok(join_student(client))["participant_id"]
    booth.clock[0] += timedelta(days=29)
    assert ok(client.get("/api/state"))["participant_id"] == pid
    booth.clock[0] += timedelta(days=1, seconds=1)
    assert ok(client.get("/api/state"))["participant_id"] is None


def test_session_cookies_are_http_only_secure_with_kind_specific_lifetimes(booth):
    with TestClient(booth.app, base_url="https://testserver") as client:
        cookie = ok_cookie(join_student(client))
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert f"Max-Age={30 * 24 * 3600}" in cookie
    with TestClient(booth.app, base_url="https://testserver") as client:
        cookie = ok_cookie(client.post("/api/session", json={"kind": "GUEST", "nickname": "쿠키손님", "consent": True}))
        assert "HttpOnly" in cookie and "Secure" in cookie and f"Max-Age={12 * 3600}" in cookie


def test_earlier_google_student_picks_a_nickname_before_playing(booth):
    booth.open()
    booth.guest("손님별명")
    client, pid = legacy_google_student(booth)
    state = ok(client.get("/api/state"))
    assert state["participant_id"] == pid and state["needs_nickname"] is True
    error(booth.submit(client), "NICKNAME_REQUIRED")
    error(client.post("/api/me/nickname", json={"nickname": "손님별명"}), "NICKNAME_TAKEN")
    error(client.post("/api/me/nickname", json={"nickname": "school-abc"}), "INVALID_NICKNAME")
    chosen = ok(client.post("/api/me/nickname", json={"nickname": "  내별명  "}))
    assert chosen["nickname"] == "내별명" and chosen["needs_nickname"] is False
    error(client.post("/api/me/nickname", json={"nickname": "다른별명"}), "NICKNAME_LOCKED")
    assert ok(booth.submit(client))["attempt_no"] == 1
    assert ok(client.get("/api/scoreboard"))["rankings"][0]["nickname"] == "내별명"
    # guests choose their nickname when joining, not through this endpoint
    error(booth.guest().post("/api/me/nickname", json={"nickname": "새손님별명"}), "FORBIDDEN")
    error(TestClient(booth.app).post("/api/me/nickname", json={"nickname": "로그인안함"}), "UNAUTHORIZED")


def test_earlier_google_student_continues_with_id_and_name(booth):
    booth.open()
    old, pid = legacy_google_student(booth, name="홍길동")
    ok(old.post("/api/me/nickname", json={"nickname": "번개전술"}))
    ok(booth.submit(old))
    session = ok(join_student(booth.client(), name="홍길동", nickname="무시됨"))
    assert session["participant_id"] == pid and session["nickname"] == "번개전술" and session["attempts_left"] == 0
    # one who never chose a nickname gets the one typed into the form
    _, other = legacy_google_student(booth, student_id="11-111", name="김철수")
    session = ok(join_student(booth.client(), student_id="11111", name="김철수", nickname="새별명"))
    assert session["participant_id"] == other and session["nickname"] == "새별명" and session["needs_nickname"] is False
    error(join_student(booth.client(), student_id="11-111", name="박영수", nickname="가로채기"), "STUDENT_ID_CONFLICT")


def test_legacy_migration_preserves_attempts_rewards_and_revokes_old_cookie(booth):
    from server import db
    booth.tries(3)
    booth.open()
    legacy = booth.guest("예전공개별명")
    participant_id = ok(legacy.get("/api/state"))["participant_id"]
    submitted = ok(booth.submit(legacy))
    with db.transaction(write=True) as c:
        c.execute("UPDATE participants SET kind='STUDENT',student_id=?,real_name=? WHERE id=?",
                  ("00-000", "예전미인증실명", participant_id))
        c.execute("UPDATE sessions SET role='PLAYER' WHERE participant_id=?", (participant_id,))
        db.set_config(c, "board_cache", "{}")
    assert ok(legacy.get("/api/state"))["participant_id"] is None
    error(legacy.get("/api/me/submissions"), "UNAUTHORIZED")
    before = ok(legacy.get("/api/scoreboard"))
    assert "예전미인증실명" not in json.dumps(before, ensure_ascii=False)
    assert before["rankings"][0]["nickname"] == "예전공개별명"
    verified, response = booth.student(name="예전미인증실명", student_id="00-000", nickname="새학생별명")
    result = ok(response)
    assert result["participant_id"] == participant_id
    assert result["nickname"] == "새학생별명"
    assert result["attempts_left"] == 2
    history = ok(verified.get("/api/me/submissions"))["items"]
    assert history[0]["id"] == submitted["id"]
    assert history[0]["reward"]["entitlement_id"] == submitted["reward"]["entitlement_id"]
    assert ok(legacy.get("/api/state"))["participant_id"] is None
    error(legacy.get("/api/me/submissions"), "UNAUTHORIZED")
    assert ok(booth.submit(verified))["attempt_no"] == 2
    after = ok(verified.get("/api/scoreboard"))
    assert after["rankings"][0]["nickname"] == "새학생별명"
    claims = ok(booth.admin.get("/api/admin/claims"))["items"]
    assert claims[0]["nickname"] == "새학생별명"
    people = ok(booth.admin.get("/api/admin/participants"))["items"]
    assert people[0]["nickname"] == "새학생별명"
    dumped = json.dumps([after, claims, people], ensure_ascii=False)
    assert "예전미인증실명" not in dumped
    assert "00-000" not in dumped and "ksa.hs.kr" not in dumped


def test_guest_expiry_and_no_recovery_by_nickname(booth):
    from server import db
    from datetime import datetime
    guest = booth.guest("임시손님")
    pid = ok(guest.get("/api/state"))["participant_id"]
    error(booth.client().post("/api/session", json={"kind": "GUEST",
        "nickname": "임시손님", "consent": True}), "NICKNAME_TAKEN")
    with db.transaction() as c:
        row = c.one("SELECT * FROM sessions WHERE participant_id=?", (pid,))
        assert row["role"] == "GUEST"
        assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"]) == timedelta(hours=12)
    booth.clock[0] += timedelta(hours=12, seconds=1)
    assert ok(guest.get("/api/state"))["participant_id"] is None
    error(guest.get("/api/me/submissions"), "UNAUTHORIZED")


def test_legacy_guest_cannot_keep_original_thirty_day_lifetime(booth):
    from server import db
    guest = booth.guest("예전外部人")
    pid = ok(guest.get("/api/state"))["participant_id"]
    with db.transaction(write=True) as c:
        c.execute("UPDATE sessions SET role='PLAYER',expires_at=? WHERE participant_id=?",
            ((booth.clock[0] + timedelta(days=30)).isoformat(), pid))
    assert ok(guest.get("/api/state"))["participant_id"] == pid
    booth.clock[0] += timedelta(hours=13)
    assert ok(guest.get("/api/state"))["participant_id"] is None


def test_guest_reentry_cannot_reset_attempts_or_extend_session(booth):
    from server import db
    booth.open()
    guest = booth.guest("원래손님")
    pid = ok(guest.get("/api/state"))["participant_id"]
    ok(booth.submit(guest))
    with db.transaction() as c:
        original_expiry = c.scalar("SELECT expires_at FROM sessions WHERE participant_id=?", (pid,))
    booth.clock[0] += timedelta(hours=1)
    result = ok(guest.post("/api/session", json={"kind": "GUEST", "nickname": "새로운별명", "consent": True}))
    assert result["participant_id"] == pid
    assert result["attempts_left"] == 0
    assert result["nickname"] == "원래손님"
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM participants") == 1
        assert c.scalar("SELECT expires_at FROM sessions WHERE participant_id=?", (pid,)) == original_expiry


def test_frozen_rank_names_stay_nicknames_and_hide_without_identity_export(booth):
    booth.open()
    student, response = booth.student(name="첫등록이름", nickname="명예별명")
    participant_id = ok(response)["participant_id"]
    ok(booth.submit(student))
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    frozen = ok(booth.admin.post("/api/admin/freeze", json={"scope": "DAY1"}))
    _, response = booth.student(name="바뀐등록이름")
    error(response, "STUDENT_ID_CONFLICT")
    current = ok(student.get("/api/hall?scope=DAY1"))
    assert current["rankings"][0]["nickname"] == "명예별명"
    assert "첫등록이름" not in json.dumps(current, ensure_ascii=False) and "바뀐등록이름" not in json.dumps(current, ensure_ascii=False)
    assert current["rankings"][0]["submission_id"] == frozen["rankings"][0]["submission_id"]
    assert current["rankings"][0]["points"] == frozen["rankings"][0]["points"]
    # The route is operational anonymous JSON, not a credentials backup.
    exported_text = json.dumps(ok(booth.admin.get("/api/admin/backup")), ensure_ascii=False)
    for private in ("첫등록이름", "바뀐등록이름", "00-000"):
        assert private not in exported_text
    ok(booth.admin.post(f"/api/admin/participants/{participant_id}/hide", json={"reason": "공개 표시 가리기 테스트"}))
    assert ok(student.get("/api/hall?scope=DAY1"))["rankings"][0]["nickname"] == "익명 전술가"


def test_old_sqlite_schema_is_upgraded_without_replacing_participants(tmp_path, monkeypatch):
    import sqlite3
    from server import db
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DB_PATH", str(path))
    with sqlite3.connect(path) as c:
        c.executescript("""
        CREATE TABLE participants (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,student_id TEXT UNIQUE,real_name TEXT,
          nickname TEXT NOT NULL UNIQUE,name_hidden INTEGER NOT NULL DEFAULT 0,
          consent_at TEXT,consent_version TEXT NOT NULL DEFAULT '2026-10-cloud-v1');
        CREATE TABLE sessions (
          token_hash TEXT PRIMARY KEY,participant_id TEXT,role TEXT NOT NULL,
          expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
        INSERT INTO participants(id,kind,student_id,real_name,nickname)
          VALUES('legacy-owner','STUDENT','00-000','기존미확인실명','기존별명');
        """)
    db.initialize(force=True)
    with db.transaction() as c:
        assert c.one("SELECT * FROM participants WHERE id='legacy-owner'")["nickname"] == "기존별명"
        assert c.scalar("SELECT COUNT(*) FROM school_identities") == 0
        assert c.scalar("SELECT COUNT(*) FROM google_login_challenges") == 0


@pytest.mark.parametrize("operation,confirm", [("purge", "PURGE"), ("reset", "RESET")])
def test_cleanup_removes_identities_challenges_and_player_sessions(booth, operation, confirm):
    from server import db
    student, response = booth.student()
    ok(response)
    legacy_google_student(booth, student_id="11-111", name="예전학생")
    ok(booth.admin.post(f"/api/admin/{operation}", json={"confirm": confirm, "reason": "인증 데이터 정리 테스트"}))
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM school_identities") == 0
        assert c.scalar("SELECT COUNT(*) FROM google_login_challenges") == 0
        assert c.scalar("SELECT COUNT(*) FROM sessions WHERE role!='ADMIN'") == 0
    assert ok(student.get("/api/state"))["participant_id"] is None


def test_admin_submission_log_carries_student_id_and_name_for_hover(booth):
    from test_backend import PIN
    booth.open()
    student = booth.client()
    ok(join_student(student, student_id="26001", name="홍길동", nickname="번개전술"))
    ok(booth.submit(student))
    ok(booth.submit(booth.guest("손님별명")))
    rows = {r["nickname"]: r for r in ok(booth.admin.get("/api/admin/submissions"))["items"]}
    assert (rows["번개전술"]["student_id"], rows["번개전술"]["real_name"]) == ("26-001", "홍길동")
    assert (rows["손님별명"]["student_id"], rows["손님별명"]["real_name"]) == (None, None)
    # admin-only: players get 401, and public pages still carry nicknames only
    error(student.get("/api/admin/submissions"), "UNAUTHORIZED")
    public = json.dumps([ok(student.get("/api/scoreboard")), ok(student.get("/api/me/submissions"))], ensure_ascii=False)
    assert "홍길동" not in public and "26-001" not in public
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    ok(booth.admin.post("/api/admin/purge", json={"confirm": "PURGE", "reason": "테스트 행사 종료"}))
    ok(booth.admin.post("/api/admin/login", json={"pin": PIN}))
    after = json.dumps(ok(booth.admin.get("/api/admin/submissions?day=ALL")), ensure_ascii=False)
    assert "홍길동" not in after and "26-001" not in after
