"""School identity, session isolation, migration and real Google signature checks.

DBs are disposable. App tests stub only the verified-token boundary. The final
tests use Google's real verifier with RSA signatures and a mocked cert response.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from test_backend import booth, ok, error
from server.google_auth import verify_credential as _real_verify_credential


def google_login(client, credential, **extra):
    return client.post("/api/auth/google", json={"credential": credential, "consent": True, **extra})


def test_configuration_and_old_student_endpoint_closed(booth, monkeypatch):
    client = booth.client()
    config = ok(client.get("/api/auth/config"))
    assert config["configured"] is True
    assert config["google_client_id"] == "test-client.apps.googleusercontent.com"
    assert config["school_domain"] == "ksa.hs.kr"
    assert config["guest_session_hours"] == 12
    error(client.post("/api/session", json={"kind": "STUDENT", "student_id": "00-000",
        "real_name": "등록이름", "nickname": "별명", "consent": True}), "GOOGLE_LOGIN_REQUIRED")
    monkeypatch.delenv("GOOGLE_CLIENT_ID")
    assert ok(client.get("/api/auth/config"))["configured"] is False
    error(client.post("/api/auth/google/challenge", json={"consent": True}), "GOOGLE_AUTH_NOT_CONFIGURED")
    assert ok(booth.guest().get("/api/state"))["participant_id"]


def test_google_challenge_requires_consent_and_browser_binding(booth):
    a, b = booth.client(), booth.client()
    error(a.post("/api/auth/google/challenge", json={"consent": False}), "CONSENT_REQUIRED")
    credential = booth.google_token(a)
    error(google_login(b, credential), "AUTH_CHALLENGE_EXPIRED")
    assert ok(b.get("/api/state"))["participant_id"] is None
    assert ok(google_login(a, credential))["nickname"] == "테스트학생"


def test_google_challenge_single_use_under_parallel_replay(booth):
    client = booth.client()
    credential = booth.google_token(client)
    a, b = booth.client(), booth.client()
    a.cookies.update(client.cookies)
    b.cookies.update(client.cookies)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda c: google_login(c, credential), (a, b)))
    assert sorted(r.status_code for r in responses) == [200, 401]
    error(next(r for r in responses if r.status_code != 200), "AUTH_CHALLENGE_EXPIRED")


def test_challenge_expires_and_replacement_invalidates_previous(booth):
    client = booth.client()
    first = booth.google_token(client)
    second = booth.google_token(client)
    assert google_login(client, first).status_code == 401
    error(google_login(client, second), "AUTH_CHALLENGE_EXPIRED")
    expired = booth.google_token(client)
    booth.clock[0] += timedelta(minutes=11)
    error(google_login(client, expired), "AUTH_CHALLENGE_EXPIRED")


@pytest.mark.parametrize("claims", [
    {"email": "00-000@gmail.com"},
    {"email": "00-000@ksa.hs.kr.attacker.example"},
    {"email": "student@ksa.hs.kr"},
    {"email": "00-000+alias@ksa.hs.kr"},
    {"email": "００-０００@ksa.hs.kr"},
    {"email_verified": False},
    {"email_verified": "true"},
    {"hd": "attacker.example"},
    {"hd": None},
])
def test_school_claims_are_enforced_by_server(booth, claims):
    client = booth.client()
    credential = booth.google_token(client, claims=claims)
    assert google_login(client, credential).status_code in (401, 403)
    assert ok(client.get("/api/state"))["participant_id"] is None
    from server import db
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM school_identities") == 0


@pytest.mark.parametrize("claims", [
    {"aud": "wrong-client.apps.googleusercontent.com"},
    {"iss": "https://attacker.example"},
    {"exp": 1},
    {"iat": 9999999999},
    {"sub": ""},
    {"name": ""},
    {"nonce": "wrong-browser-nonce"},
])
def test_unusable_token_claims_never_create_accounts(booth, claims):
    client = booth.client()
    credential = booth.google_token(client, claims=claims)
    assert google_login(client, credential).status_code in (400, 401, 403)
    error(google_login(client, credential), "AUTH_CHALLENGE_EXPIRED")
    assert ok(client.get("/api/state"))["participant_id"] is None


def test_identity_and_name_come_from_token_not_request(booth):
    client = booth.client()
    credential = booth.google_token(client, name="학교등록이름", student_id="00-000")
    result = ok(google_login(client, credential, nickname="허위별명", real_name="허위실명",
        email="99-999@ksa.hs.kr", student_id="99-999", sub="attacker"))
    assert result["nickname"] == "학교등록이름"
    from server import db
    with db.transaction() as c:
        p = c.one("SELECT * FROM participants WHERE id=?", (result["participant_id"],))
        assert p["student_id"] == "00-000"
        assert p["real_name"] == "학교등록이름"
        identity = c.one("SELECT * FROM school_identities WHERE participant_id=?", (p["id"],))
        assert identity["school_email"] == "00-000@ksa.hs.kr"
        assert identity["google_sub"] == "google-sub-00-000"


def test_duplicate_real_names_and_public_name_refresh(booth):
    booth.open()
    a, ra = booth.student(name="동명이인", student_id="00-000", nickname="첫째별명")
    b, rb = booth.student(name="동명이인", student_id="24-103", nickname="둘째별명")
    aid, bid = ok(ra)["participant_id"], ok(rb)["participant_id"]
    assert aid != bid
    ok(booth.submit(a))
    ok(booth.submit(b))
    rows = ok(a.get("/api/scoreboard"))["rankings"]
    assert {r["nickname"] for r in rows if r["participant_id"] in (aid, bid)} == {"첫째별명", "둘째별명"}
    assert "동명이인" not in json.dumps(rows, ensure_ascii=False)
    # a changed Google account name never reaches the public board
    _, updated = booth.student(name="새등록이름", student_id="00-000")
    assert ok(updated)["participant_id"] == aid
    rows = ok(a.get("/api/scoreboard"))["rankings"]
    assert next(r["nickname"] for r in rows if r["participant_id"] == aid) == "첫째별명"
    assert "새등록이름" not in json.dumps(rows, ensure_ascii=False)


def test_student_must_choose_unique_nickname_once_before_playing(booth):
    booth.open()
    booth.guest("손님별명")
    client, response = booth.student(choose=False)
    session = ok(response)
    assert session["needs_nickname"] is True
    state = ok(client.get("/api/state"))
    assert state["needs_nickname"] is True
    error(booth.submit(client), "NICKNAME_REQUIRED")
    error(client.post("/api/me/nickname", json={"nickname": "손님별명"}), "NICKNAME_TAKEN")
    error(client.post("/api/me/nickname", json={"nickname": "school-abc"}), "INVALID_NICKNAME")
    error(client.post("/api/me/nickname", json={"nickname": "a"}), "INVALID_NICKNAME")
    chosen = ok(client.post("/api/me/nickname", json={"nickname": "  내별명  "}))
    assert chosen["nickname"] == "내별명" and chosen["needs_nickname"] is False
    assert ok(client.get("/api/state"))["needs_nickname"] is False
    error(client.post("/api/me/nickname", json={"nickname": "다른별명"}), "NICKNAME_LOCKED")
    assert ok(booth.submit(client))["attempt_no"] == 1
    assert ok(client.get("/api/scoreboard"))["rankings"][0]["nickname"] == "내별명"
    # guests choose their nickname when joining, not through this endpoint
    guest = booth.guest()
    error(guest.post("/api/me/nickname", json={"nickname": "새손님별명"}), "FORBIDDEN")
    error(TestClient(booth.app).post("/api/me/nickname", json={"nickname": "로그인안함"}), "UNAUTHORIZED")


def test_verified_email_cannot_be_rebound_to_another_google_sub(booth):
    first, response = booth.student(student_id="00-000")
    identity = ok(response)
    attacker = booth.client()
    credential = booth.google_token(attacker, student_id="00-000", sub="different-google-sub")
    error(google_login(attacker, credential), "ACCOUNT_CONFLICT")
    assert ok(first.get("/api/state"))["participant_id"] == identity["participant_id"]


def test_legacy_migration_preserves_attempts_rewards_and_revokes_old_cookie(booth):
    from server import db
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
    verified, response = booth.student(name="학교에서받은이름", student_id="00-000", nickname="새학생별명")
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
    assert "학교에서받은이름" not in dumped
    assert "00-000" not in dumped and "ksa.hs.kr" not in dumped
    assert "google-sub-" not in dumped


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
    assert result["attempts_left"] == 2
    assert result["nickname"] == "원래손님"
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM participants") == 1
        assert c.scalar("SELECT expires_at FROM sessions WHERE participant_id=?", (pid,)) == original_expiry


def test_frozen_rank_names_refresh_and_hide_without_identity_export(booth):
    booth.open()
    student, response = booth.student(name="첫등록이름", nickname="명예별명")
    participant_id = ok(response)["participant_id"]
    ok(booth.submit(student))
    ok(booth.admin.post("/api/admin/booth", json={"action": "CLOSE"}))
    frozen = ok(booth.admin.post("/api/admin/freeze", json={"scope": "DAY1"}))
    _, response = booth.student(name="바뀐등록이름")
    ok(response)
    current = ok(student.get("/api/hall?scope=DAY1"))
    assert current["rankings"][0]["nickname"] == "명예별명"
    assert "첫등록이름" not in json.dumps(current, ensure_ascii=False) and "바뀐등록이름" not in json.dumps(current, ensure_ascii=False)
    assert current["rankings"][0]["submission_id"] == frozen["rankings"][0]["submission_id"]
    assert current["rankings"][0]["points"] == frozen["rankings"][0]["points"]
    # The route is operational anonymous JSON, not a credentials backup.
    exported_text = json.dumps(ok(booth.admin.get("/api/admin/backup")), ensure_ascii=False)
    for private in ("첫등록이름", "바뀐등록이름", "00-000", "ksa.hs.kr", "google-sub-00-000"):
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
    booth.google_token(booth.client())
    ok(booth.admin.post(f"/api/admin/{operation}", json={"confirm": confirm, "reason": "인증 데이터 정리 테스트"}))
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM school_identities") == 0
        assert c.scalar("SELECT COUNT(*) FROM google_login_challenges") == 0
        assert c.scalar("SELECT COUNT(*) FROM sessions WHERE role!='ADMIN'") == 0
    assert ok(student.get("/api/state"))["participant_id"] is None


def test_auth_cookies_are_http_only_secure_on_https(booth):
    from fastapi.testclient import TestClient
    with TestClient(booth.app, base_url="https://testserver") as client:
        response = client.post("/api/auth/google/challenge", json={"consent": True})
        nonce = ok(response)["nonce"]
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert "Max-Age=600" in cookie
        token = "secure-cookie-fixture"
        booth.credentials[token] = {
            "aud": "test-client.apps.googleusercontent.com", "iss": "https://accounts.google.com",
            "exp": int(time.time()) + 600, "iat": int(time.time()), "sub": "google-sub-00-000",
            "hd": "ksa.hs.kr", "email": "00-000@ksa.hs.kr", "email_verified": True,
            "name": "학교등록이름", "nonce": nonce,
        }
        response = google_login(client, token)
        ok(response)
        cookies = ";".join(response.headers.get_list("set-cookie"))
        assert "HttpOnly" in cookies and "Secure" in cookies and "SameSite=lax" in cookies
        assert "Max-Age=2592000" in cookies


def test_verification_network_failure_is_closed_and_nonce_is_consumed(booth, monkeypatch):
    from server import google_auth
    def unavailable(*args, **kwargs):
        raise google_auth.GoogleVerificationError(unavailable=True)
    monkeypatch.setattr(google_auth, "verify_credential", unavailable)
    client = booth.client()
    credential = booth.google_token(client)
    error(google_login(client, credential), "GOOGLE_AUTH_UNAVAILABLE")
    assert ok(client.get("/api/state"))["participant_id"] is None
    error(google_login(client, credential), "AUTH_CHALLENGE_EXPIRED")


@pytest.fixture
def signed_google_token(monkeypatch):
    """Real Google verifier; local RSA signing key and mocked certificate fetch."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from google.auth.transport.requests import Request
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    fetches = []
    def certificates(self, url, **kwargs):
        fetches.append((url, kwargs))
        return SimpleNamespace(status=200, data=json.dumps({"test-key": public_pem}).encode())
    monkeypatch.setattr(Request, "__call__", certificates)
    def encoded(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=")
    def make(changes=None, wrong_key=False):
        claims = {"iss": "https://accounts.google.com", "aud": "test-client.apps.googleusercontent.com",
            "sub": "google-sub-00-000", "iat": int(time.time()), "exp": int(time.time()) + 600,
            "email": "00-000@ksa.hs.kr", "email_verified": True, "hd": "ksa.hs.kr", "name": "학교등록이름"}
        if changes: claims.update(changes)
        signing = encoded(json.dumps({"alg": "RS256", "kid": "test-key"}).encode()) + b"." + encoded(json.dumps(claims).encode())
        signer = rsa.generate_private_key(public_exponent=65537, key_size=2048) if wrong_key else key
        signature = signer.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        return (signing + b"." + encoded(signature)).decode()
    return make, fetches


def test_real_verifier_accepts_signed_token_with_bounded_network(signed_google_token):
    from server.google_auth import verify_credential
    make, fetches = signed_google_token
    result = verify_credential(make(), "test-client.apps.googleusercontent.com")
    assert result["sub"] == "google-sub-00-000"
    assert fetches and all(call[1]["timeout"] == (3, 7) for call in fetches)


def test_api_uses_real_signature_verification_before_creating_session(booth, signed_google_token, monkeypatch):
    from server import google_auth, db
    monkeypatch.setattr(google_auth, "verify_credential", _real_verify_credential)
    make, _ = signed_google_token
    client = booth.client()
    nonce = ok(client.post("/api/auth/google/challenge", json={"consent": True}))["nonce"]
    error(google_login(client, make({"nonce": nonce}, wrong_key=True)), "GOOGLE_AUTH_INVALID")
    with db.transaction() as c:
        assert c.scalar("SELECT COUNT(*) FROM school_identities") == 0
    assert ok(client.get("/api/state"))["participant_id"] is None
    fresh_nonce = ok(client.post("/api/auth/google/challenge", json={"consent": True}))["nonce"]
    result = ok(google_login(client, make({"nonce": fresh_nonce})))
    state = ok(client.get("/api/state"))
    assert result["participant_id"] == state["participant_id"]
    assert state["auth_method"] == "GOOGLE" and state["nickname"] == "학교등록이름"


@pytest.mark.parametrize("case", ["signature", "audience", "issuer", "expired", "future", "malformed"])
def test_real_verifier_rejects_invalid_tokens(signed_google_token, case):
    from server.google_auth import verify_credential, GoogleVerificationError
    make, _ = signed_google_token
    changes = {"audience": {"aud": "another-client"}, "issuer": {"iss": "https://attacker.example"},
        "expired": {"exp": int(time.time()) - 60}, "future": {"iat": int(time.time()) + 3600}}
    token = "not.a.jwt" if case == "malformed" else make(changes.get(case), wrong_key=case == "signature")
    with pytest.raises(GoogleVerificationError) as caught:
        verify_credential(token, "test-client.apps.googleusercontent.com")
    assert caught.value.unavailable is False

