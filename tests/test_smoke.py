import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app


client = TestClient(app.app)


def register_verified(test_client: TestClient, identifier: str, phone: str) -> object:
    response = test_client.post(
        "/register",
        data={
            "identifier": identifier,
            "full_name": "Test Student",
            "age_range": "16–17",
            "standard": "Grade 10",
            "phone": phone,
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert "dev_code" not in query
    with app.db() as connection:
        challenge = connection.execute(
            "SELECT dev_code FROM registration_challenges WHERE token = ?", (query["token"][0],)
        ).fetchone()
    return test_client.post(
        "/verify-phone",
        data={"token": query["token"][0], "code": challenge["dev_code"]},
        follow_redirects=False,
    )


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "study_buddy.db")
    app._rate_limit_hits.clear()
    app.init_db()


def test_public_pages_and_health() -> None:
    assert client.get("/").status_code == 200
    assert client.get("/register").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok", "service": "study-buddy"}


def test_protected_pages_redirect_without_session() -> None:
    for path in ("/dashboard", "/general", "/rag", "/voice"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]


def test_active_templates_and_assets_exist() -> None:
    required_templates = (
        "auth/login_slow.html",
        "auth/register.html",
        "pages/dashboard4.html",
        "pages/general_chat.html",
        "pages/rag_workspace.html",
        "pages/dashboard3.html",
        "admin/admin.html",
    )
    for template in required_templates:
        assert (app.BASE_DIR / "templates" / template).exists()
    assert (app.BASE_DIR / "static" / "css" / "styles.css").exists()
    assert (app.BASE_DIR / "static" / "images" / "study-buddy-logo.png").exists()


def test_frontend_handlers_are_present() -> None:
    pages = {
        "pages/dashboard4.html": ("chat-form", "upload-form", "quiz-button"),
        "pages/general_chat.html": ("chat-form", "data-prompt"),
        "pages/rag_workspace.html": ("upload-form", "/api/documents", "/api/rag/chat"),
        "pages/dashboard3.html": ("start-voice", "stop-voice", "new WebSocket"),
    }
    for template, markers in pages.items():
        contents = (app.BASE_DIR / "templates" / template).read_text(encoding="utf-8")
        assert all(marker in contents for marker in markers)
    login = (app.BASE_DIR / "templates" / "auth/login_slow.html").read_text(encoding="utf-8")
    assert 'class="password-toggle"' in login
    assert 'role="alert"' in login
    assert ".quote-copy" in login
    assert "loginFieldIn" in login
    assert "nth-of-type(2)" in login
    assert "cardFromLeft 2s" in login
    shared_styles = (app.BASE_DIR / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert "body.dashboard button" in shared_styles
    assert "border-radius:999px" in shared_styles
    assert "@keyframes cardFromLeft" in shared_styles
    assert "@keyframes panelFromRight" in shared_styles
    escalation = (app.BASE_DIR / "static" / "js/escalation.js").read_text(encoding="utf-8")
    assert "unresolved" in escalation
    assert "Start voice teacher" in escalation
    assert "Keep typing" in escalation
    assert "/voice?reason=stuck" in escalation
    assert "orbBubble" in shared_styles
    assert "min-height:58px!important" in shared_styles
    assert "user-speaking" in shared_styles
    assert "user-speaking" in (app.BASE_DIR / "templates/pages/dashboard3.html").read_text(encoding="utf-8")
    assert "buddyBubble" in shared_styles
    assert ".logo-bubble:hover" in shared_styles


def test_escalation_script_is_loaded_on_chat_surfaces() -> None:
    general = TestClient(app.app).get("/general", follow_redirects=False)
    assert general.status_code == 303
    for template in ("pages/rag_workspace.html", "pages/dashboard4.html"):
        contents = (app.BASE_DIR / "templates" / template).read_text(encoding="utf-8")
        assert "/static/js/escalation.js" in contents


def test_registration_login_and_user_isolation(monkeypatch) -> None:
    first = TestClient(app.app)
    second = TestClient(app.app)
    assert register_verified(first, "first@example.com", "+919876543201").status_code == 303
    assert register_verified(second, "second@example.com", "+919876543202").status_code == 303
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="": "test answer")
    csrf = {"X-CSRF-Token": first.cookies["csrf_token"]}
    assert first.post("/api/chat", data={"message": "first secret"}, headers=csrf).status_code == 200
    assert second.get("/api/chat").json()["messages"] == []
    assert first.get("/api/chat").json()["messages"][0]["message"] == "first secret"


def test_phone_verification_required_and_profile_persists() -> None:
    verification_client = TestClient(app.app)
    response = verification_client.post(
        "/register",
        data={
            "identifier": "verified@example.com",
            "full_name": "Verified Student",
            "age_range": "16–17",
            "standard": "Grade 11",
            "phone": "+919876543209",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    token = query["token"][0]
    verification_page = verification_client.get(response.headers["location"])
    assert verification_page.status_code == 200
    assert "Age range" not in verification_page.text
    assert "Available in" in verification_page.text
    assert "+91******3209" in verification_page.text
    resend = verification_client.post("/verify-phone/resend", data={"token": token}, follow_redirects=False)
    assert resend.status_code == 303
    assert "Please+wait" in resend.headers["location"] or "Please%20wait" in resend.headers["location"]
    wrong = verification_client.post("/verify-phone", data={"token": token, "code": "999999"}, follow_redirects=False)
    assert wrong.status_code == 303
    assert "/verify-phone" in wrong.headers["location"]
    assert verification_client.get("/dashboard", follow_redirects=False).status_code == 303
    with app.db() as connection:
        code = connection.execute(
            "SELECT dev_code FROM registration_challenges WHERE token = ?", (token,)
        ).fetchone()["dev_code"]
    verified = verification_client.post("/verify-phone", data={"token": token, "code": code}, follow_redirects=False)
    assert verified.status_code == 303
    with app.db() as connection:
        user = connection.execute(
            "SELECT full_name, age_range, standard, phone, phone_verified FROM users WHERE identifier = ?",
            ("verified@example.com",),
        ).fetchone()
    assert tuple(user) == ("Verified Student", "16–17", "Grade 11", "+919876543209", 1)


def test_authenticated_pages_render_after_login() -> None:
    authenticated = TestClient(app.app)
    registration = register_verified(authenticated, "smoke@example.com", "+919876543203")
    assert registration.status_code == 303
    assert "/dashboard" in registration.headers["location"]
    assert authenticated.get("/dashboard").status_code == 200
    assert authenticated.get("/general").status_code == 200
    assert authenticated.get("/rag").status_code == 200
    assert authenticated.get("/voice").status_code == 200


def test_profile_update_and_phone_change_require_verification() -> None:
    profile_client = TestClient(app.app)
    register_verified(profile_client, "profile@example.com", "+919876543210")
    csrf = {"X-CSRF-Token": profile_client.cookies["csrf_token"]}
    assert profile_client.get("/profile").status_code == 200
    updated = profile_client.post(
        "/profile",
        data={"full_name": "Updated Student", "age_range": "18–24", "standard": "University"},
        headers=csrf,
        follow_redirects=False,
    )
    assert updated.status_code == 303
    changed = profile_client.post(
        "/profile/phone",
        data={"phone": "+919876543211"},
        headers=csrf,
        follow_redirects=False,
    )
    query = parse_qs(urlparse(changed.headers["location"]).query)
    with app.db() as connection:
        challenge = connection.execute(
            "SELECT dev_code FROM registration_challenges WHERE token = ?", (query["token"][0],)
        ).fetchone()
    verified = profile_client.post(
        "/verify-phone", data={"token": query["token"][0], "code": challenge["dev_code"]}, follow_redirects=False
    )
    assert verified.headers["location"].startswith("/profile")
    with app.db() as connection:
        user = connection.execute(
            "SELECT full_name, age_range, standard, phone, phone_verified FROM users WHERE identifier = ?",
            ("profile@example.com",),
        ).fetchone()
    assert tuple(user) == ("Updated Student", "18–24", "University", "+919876543211", 1)


def test_twilio_delivery_payload_and_missing_credentials(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = parse_qs(request.data.decode())
        captured["auth"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(app, "SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_PHONE", "+15550000000")
    monkeypatch.setattr(app, "urlopen", fake_urlopen)
    assert app.deliver_verification_code("+919876543212", "123456") is True
    assert captured["url"].endswith("/Accounts/ACtest/Messages.json")
    assert captured["body"]["To"] == ["+919876543212"]
    assert captured["body"]["From"] == ["+15550000000"]
    assert "123456" in captured["body"]["Body"][0]
    assert captured["auth"].startswith("Basic ")
    assert captured["timeout"] == 10

    monkeypatch.delenv("TWILIO_ACCOUNT_SID")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN")
    monkeypatch.delenv("TWILIO_FROM_PHONE")
    assert app.deliver_verification_code("+919876543212", "123456") is False


def test_twilio_delivery_supports_api_key_credentials(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        captured["auth"] = request.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(app, "SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SKtest")
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "api-secret")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TWILIO_FROM_PHONE", "+15550000000")
    monkeypatch.setattr(app, "urlopen", fake_urlopen)

    assert app.deliver_verification_code("+919876543212", "123456") is True
    expected = base64.b64encode(b"SKtest:api-secret").decode()
    assert captured["auth"] == f"Basic {expected}"


def test_plain_chat_and_rag_chat_have_separate_context_paths(monkeypatch) -> None:
    chat_client = TestClient(app.app)
    register_verified(chat_client, "paths@example.com", "+919876543204")
    with app.db() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("paths@example.com",)).fetchone()["id"]
        connection.execute("INSERT INTO documents (user_id, filename, content, created_at) VALUES (?, ?, ?, ?)", (user_id, "biology.pdf", "[Source: biology.pdf, page 2]\nMitochondria make cellular energy.", app.utc_now()))
    calls = []
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="": calls.append((prompt, context)) or "answer")
    monkeypatch.setattr(app, "semantic_scores", lambda query, chunks: [0.0] * len(chunks))
    csrf = {"X-CSRF-Token": chat_client.cookies["csrf_token"]}
    assert chat_client.post("/api/chat", data={"message": "What is energy?"}, headers=csrf).status_code == 200
    assert chat_client.post("/api/rag/chat", data={"message": "What do mitochondria do?"}, headers=csrf).status_code == 200
    assert calls[0][1] == ""
    assert "biology.pdf" in calls[1][1]
    assert "page 2" in calls[1][1]


def test_mutating_api_requires_authentication() -> None:
    client.get("/api/health")
    response = client.post("/api/chat", data={"message": "hello"}, headers={"X-CSRF-Token": client.cookies["csrf_token"]})
    assert response.status_code == 401


def test_upload_limits_and_pdf_signature() -> None:
    register_verified(client, "reader@example.com", "+919876543205")
    invalid = client.post("/api/documents", files={"file": ("notes.pdf", b"not a pdf", "application/pdf")}, headers={"X-CSRF-Token": client.cookies["csrf_token"]})
    assert invalid.status_code == 400


def test_document_delete_is_user_scoped() -> None:
    owner = TestClient(app.app)
    other = TestClient(app.app)
    register_verified(owner, "owner@example.com", "+919876543206")
    register_verified(other, "other@example.com", "+919876543207")
    with app.db() as connection:
        owner_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("owner@example.com",)).fetchone()["id"]
        document_id = connection.execute("INSERT INTO documents (user_id, filename, content, created_at) VALUES (?, ?, ?, ?)", (owner_id, "notes.pdf", "text", app.utc_now())).lastrowid
    other_response = other.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": other.cookies["csrf_token"]})
    assert other_response.status_code == 404
    owner_response = owner.delete(f"/api/documents/{document_id}", headers={"X-CSRF-Token": owner.cookies["csrf_token"]})
    assert owner_response.status_code == 200


def test_quiz_submission_scores_and_persists() -> None:
    quiz_client = TestClient(app.app)
    register_verified(quiz_client, "quiz@example.com", "+919876543208")
    questions = [{"question": "2 + 2?", "options": ["3", "4", "5", "6"], "answer": 1, "explanation": "Basic addition."}]
    with app.db() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("quiz@example.com",)).fetchone()["id"]
        quiz_id = connection.execute("INSERT INTO quizzes (user_id, title, questions_json, created_at) VALUES (?, ?, ?, ?)", (user_id, "Quick review", json.dumps(questions), app.utc_now())).lastrowid
    response = quiz_client.post(f"/api/quiz/{quiz_id}/submit", data={"answers": json.dumps([1])}, headers={"X-CSRF-Token": quiz_client.cookies["csrf_token"]})
    assert response.json() == {"score": 1, "total": 1}
