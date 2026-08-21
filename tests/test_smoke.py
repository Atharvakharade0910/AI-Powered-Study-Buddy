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
    public_page = client.get("/")
    assert public_page.status_code == 200
    assert public_page.headers["x-request-id"]
    assert "Content-Security-Policy" in public_page.headers
    assert client.get("/register").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok", "service": "study-buddy"}
    readiness = client.get("/api/ready")
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"


def test_production_verification_page_selects_payload_json(monkeypatch) -> None:
    token = "production-page-token"
    with app.db() as connection:
        connection.execute(
            "INSERT INTO registration_challenges (token, phone, code_hash, payload_json, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token, "+919876543299", "hash", json.dumps({"kind": "registration"}), "2099-01-01T00:00:00+00:00", app.utc_now()),
        )
    monkeypatch.setattr(app, "PHONE_VERIFICATION_DEV_MODE", False)
    monkeypatch.setattr(app, "SMS_PROVIDER", "twilio")
    response = client.get(f"/verify-phone?token={token}")
    assert response.status_code == 200
    assert "Available in" in response.text


def test_supported_standards_are_one_through_nine() -> None:
    assert app.normalize_standard("Standard 1") == "Standard 1"
    assert app.normalize_standard("grade 9") == "Standard 9"
    assert app.normalize_standard("Standard 10") is None
    assert app.normalize_standard("FY BSc") is None


def test_registration_does_not_ask_for_standard() -> None:
    assert 'name="standard"' not in client.get("/register").text
    response = client.post(
        "/register",
        data={
            "identifier": "older@example.com",
            "full_name": "Older Student",
            "age_range": "16–17",
            "phone": "+919876543299",
            "password": "password123",
            "confirm_password": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/verify-phone?token=")


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
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="", memory="": "test answer")
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
    assert tuple(user) == ("Verified Student", "16–17", "", "+919876543209", 1)


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
        data={"full_name": "Updated Student", "age_range": "18–24", "standard": "Standard 9"},
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
    assert tuple(user) == ("Updated Student", "18–24", "Standard 9", "+919876543211", 1)


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
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="", memory="": calls.append((prompt, context, memory)) or "answer")
    monkeypatch.setattr(app, "semantic_scores", lambda query, chunks: [0.0] * len(chunks))
    csrf = {"X-CSRF-Token": chat_client.cookies["csrf_token"]}
    assert chat_client.post("/api/chat", data={"message": "What is energy?"}, headers=csrf).status_code == 200
    assert chat_client.post("/api/rag/chat", data={"message": "What do mitochondria do?"}, headers=csrf).status_code == 200
    assert calls[0][1] == ""
    assert "biology.pdf" in calls[1][1]
    assert "page 2" in calls[1][1]


def test_general_chat_streams_tokens_and_persists(monkeypatch) -> None:
    stream_client = TestClient(app.app)
    register_verified(stream_client, "stream@example.com", "+919876543240")
    monkeypatch.setattr(app, "ai_stream_answer", lambda prompt, context="", memory="": iter(["Direct answer. ", "Simple explanation."]))
    response = stream_client.post(
        "/api/chat/stream",
        data={"message": "Explain gravity"},
        headers={"X-CSRF-Token": stream_client.cookies["csrf_token"]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"text": "Direct answer. "' in response.text
    assert '"type": "done"' in response.text
    with app.db() as connection:
        saved = connection.execute("SELECT message FROM chat_messages WHERE user_id = (SELECT id FROM users WHERE identifier = ?)", ("stream@example.com",)).fetchall()
    assert saved[-1]["message"] == "Direct answer. Simple explanation."


def test_general_teacher_receives_conversation_memory(monkeypatch) -> None:
    memory_client = TestClient(app.app)
    register_verified(memory_client, "memory@example.com", "+919876543241")
    memories = []
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="", memory="": memories.append(memory) or "Remembered answer")
    csrf = {"X-CSRF-Token": memory_client.cookies["csrf_token"]}
    assert memory_client.post("/api/chat", data={"message": "My project is about planets."}, headers=csrf).status_code == 200
    assert memory_client.post("/api/chat", data={"message": "What is my project about?"}, headers=csrf).status_code == 200
    assert "My project is about planets." in memories[-1]
    assert "Remembered answer" in memories[-1]


def test_voice_memory_is_user_scoped() -> None:
    first = TestClient(app.app)
    second = TestClient(app.app)
    register_verified(first, "voice-memory-one@example.com", "+919876543242")
    register_verified(second, "voice-memory-two@example.com", "+919876543243")
    with app.db() as connection:
        first_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("voice-memory-one@example.com",)).fetchone()["id"]
        second_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("voice-memory-two@example.com",)).fetchone()["id"]
        connection.executemany("INSERT INTO voice_messages (user_id, role, message, created_at) VALUES (?, ?, ?, ?)", [(first_id, "user", "I am studying planets.", app.utc_now()), (first_id, "assistant", "Let us begin with orbits.", app.utc_now()), (second_id, "user", "I am studying cells.", app.utc_now())])
    assert "planets" in app.voice_memory(first_id)
    assert "cells" not in app.voice_memory(first_id)


def test_learner_memory_can_be_added_listed_and_deleted() -> None:
    memory_client = TestClient(app.app)
    register_verified(memory_client, "learner-memory@example.com", "+919876543244")
    csrf = {"X-CSRF-Token": memory_client.cookies["csrf_token"]}
    saved = memory_client.post("/api/learning/memory", data={"key": "Goal", "value": "Prepare for my science exam"}, headers=csrf)
    assert saved.status_code == 200
    assert saved.json()["memories"][0]["memory_value"] == "Prepare for my science exam"
    listed = memory_client.get("/api/learning/memory")
    assert listed.status_code == 200
    assert listed.json()["memories"][0]["memory_key"] == "Goal"
    deleted = memory_client.delete("/api/learning/memory/Goal", headers=csrf)
    assert deleted.status_code == 200
    assert memory_client.get("/api/learning/memory").json()["memories"] == []


def test_general_conversations_are_created_and_deleted_user_scoped() -> None:
    conversation_client = TestClient(app.app)
    register_verified(conversation_client, "general-sessions@example.com", "+919876543245")
    csrf = {"X-CSRF-Token": conversation_client.cookies["csrf_token"]}
    created = conversation_client.post("/api/conversations", data={"title": "Physics revision", "mode": "general"}, headers=csrf)
    assert created.status_code == 200
    conversation_id = created.json()["conversation"]["id"]
    listed = conversation_client.get("/api/conversations?mode=general")
    assert any(item["id"] == conversation_id for item in listed.json()["conversations"])
    deleted = conversation_client.delete(f"/api/conversations/{conversation_id}", headers=csrf)
    assert deleted.status_code == 200
    assert conversation_client.get(f"/api/chat?conversation_id={conversation_id}").status_code == 404


def test_learning_recommendation_is_account_scoped() -> None:
    recommendation_client = TestClient(app.app)
    register_verified(recommendation_client, "recommendation@example.com", "+919876543246")
    initial = recommendation_client.get("/api/learning/recommendation")
    assert initial.status_code == 200
    assert initial.json()["kind"] == "start"
    with app.db() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("recommendation@example.com",)).fetchone()["id"]
        connection.execute("INSERT INTO learning_events (user_id, event_type, topic, score, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (user_id, "quiz", "Fractions", 25, "{}", app.utc_now()))
    assert recommendation_client.get("/api/learning/recommendation").json()["kind"] == "practice"


def test_mutating_api_requires_authentication() -> None:
    client.get("/api/health")
    response = client.post("/api/chat", data={"message": "hello"}, headers={"X-CSRF-Token": client.cookies["csrf_token"]})
    assert response.status_code == 401


def test_password_reset_updates_password_and_revokes_sessions() -> None:
    reset_client = TestClient(app.app)
    register_verified(reset_client, "reset@example.com", "+919876543237")
    reset_response = reset_client.post("/forgot-password", data={"identifier": "reset@example.com"}, follow_redirects=False)
    assert reset_response.status_code == 303
    token = parse_qs(urlparse(reset_response.headers["location"]).query)["token"][0]
    with app.db() as connection:
        challenge = connection.execute("SELECT dev_code FROM registration_challenges WHERE token = ?", (token,)).fetchone()
    response = reset_client.post("/reset-password", data={"token": token, "code": challenge["dev_code"], "password": "newpassword123", "confirm_password": "newpassword123"}, follow_redirects=False)
    assert response.status_code == 303
    login = reset_client.post("/login", data={"identifier": "reset@example.com", "password": "newpassword123"}, follow_redirects=False)
    assert login.status_code == 303


def test_account_export_and_delete_are_user_scoped() -> None:
    account_client = TestClient(app.app)
    register_verified(account_client, "account-data@example.com", "+919876543238")
    exported = account_client.get("/api/export/account")
    assert exported.status_code == 200
    assert exported.json()["account"]["identifier"] == "account-data@example.com"
    deleted = account_client.request("DELETE", "/api/account", data={"confirm": "DELETE MY ACCOUNT"}, headers={"X-CSRF-Token": account_client.cookies["csrf_token"]})
    assert deleted.status_code == 200
    assert account_client.get("/dashboard", follow_redirects=False).status_code == 303


def test_upload_limits_and_pdf_signature() -> None:
    register_verified(client, "reader@example.com", "+919876543205")
    invalid = client.post("/api/documents", files={"file": ("notes.pdf", b"not a pdf", "application/pdf")}, headers={"X-CSRF-Token": client.cookies["csrf_token"]})
    assert invalid.status_code == 400


def test_upload_fails_closed_when_malware_scanner_rejects(monkeypatch) -> None:
    register_verified(client, "scan@example.com", "+919876543239")
    import fitz

    pdf = fitz.open()
    pdf.new_page()
    raw = pdf.tobytes()
    pdf.close()
    monkeypatch.setattr(app, "scan_bytes", lambda payload: (_ for _ in ()).throw(app.MalwareScanError("blocked")))
    response = client.post(
        "/api/documents",
        files={"file": ("notes.pdf", raw, "application/pdf")},
        headers={"X-CSRF-Token": client.cookies["csrf_token"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "document_security_scan_failed"


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
    assert response.json() == {
        "score": 1,
        "total": 1,
        "review_cards_created": 0,
        "cached": False,
        "results": [{"correct_answer": 1, "explanation": "Basic addition."}],
    }


def test_rag_chat_fails_closed_without_document_context(monkeypatch) -> None:
    rag_client = TestClient(app.app)
    register_verified(rag_client, "no-docs@example.com", "+919876543220")
    monkeypatch.setattr(app, "ai_answer", lambda *args, **kwargs: pytest.fail("RAG mode must not fall back to plain chat"))
    response = rag_client.post(
        "/api/rag/chat",
        data={"message": "Explain photosynthesis"},
        headers={"X-CSRF-Token": rag_client.cookies["csrf_token"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "upload_pdf_first"


def test_quiz_generation_does_not_duplicate_context(monkeypatch) -> None:
    quiz_client = TestClient(app.app)
    register_verified(quiz_client, "quiz-context@example.com", "+919876543221")
    monkeypatch.setattr(app, "context_for_user", lambda user_id: "[Source: notes.pdf, page 1]\nCell theory.")
    calls = []

    def fake_answer(prompt, context="", memory=""):
        calls.append((prompt, context))
        return json.dumps([{
            "question": "What does cell theory describe?",
            "options": ["Cells", "Stars", "Rocks", "Weather"],
            "answer": 0,
            "explanation": "It describes the basic principles of cells.",
        }])

    monkeypatch.setattr(app, "ai_answer", fake_answer)
    response = quiz_client.post(
        "/api/quiz/generate",
        headers={"X-CSRF-Token": quiz_client.cookies["csrf_token"]},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][1].count("Cell theory.") == 1
    assert "Material:" not in calls[0][1]


def test_review_creation_is_rate_limited() -> None:
    review_client = TestClient(app.app)
    register_verified(review_client, "review-limit@example.com", "+919876543222")
    headers = {"X-CSRF-Token": review_client.cookies["csrf_token"]}
    responses = [
        review_client.post("/api/reviews", data={"prompt": f"Question {index}", "answer": "Answer"}, headers=headers)
        for index in range(app.RATE_LIMITS["review"][0] + 1)
    ]
    assert all(response.status_code == 200 for response in responses[:-1])
    assert responses[-1].status_code == 429


def test_chat_history_is_bounded_to_recent_messages() -> None:
    history_client = TestClient(app.app)
    register_verified(history_client, "history@example.com", "+919876543223")
    with app.db() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("history@example.com",)).fetchone()["id"]
        connection.executemany(
            "INSERT INTO chat_messages (user_id, role, message, created_at) VALUES (?, 'user', ?, ?)",
            [(user_id, f"message-{index}", app.utc_now()) for index in range(app.MAX_CHAT_HISTORY + 10)],
        )
    messages = app.user_chat(user_id)
    assert len(messages) == app.MAX_CHAT_HISTORY
    assert messages[0]["message"] == "message-10"
    assert messages[-1]["message"] == f"message-{app.MAX_CHAT_HISTORY + 9}"


def test_frontends_define_visible_api_error_handling() -> None:
    for template in (
        "pages/dashboard4.html",
        "pages/general_chat.html",
        "pages/rag_workspace.html",
    ):
        contents = (app.BASE_DIR / "templates" / template).read_text(encoding="utf-8")
        assert "response.ok" in contents
        assert "Request failed" in contents


def test_document_conversations_are_user_scoped_and_switchable(monkeypatch) -> None:
    first = TestClient(app.app)
    second = TestClient(app.app)
    register_verified(first, "conversation-first@example.com", "+919876543224")
    register_verified(second, "conversation-second@example.com", "+919876543225")
    csrf = {"X-CSRF-Token": first.cookies["csrf_token"]}
    created = first.post(
        "/api/conversations",
        data={"title": "Biology revision", "mode": "document"},
        headers=csrf,
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation"]["id"]
    with app.db() as connection:
        user_id = connection.execute(
            "SELECT id FROM users WHERE identifier = ?", ("conversation-first@example.com",)
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO documents (user_id, filename, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "biology.pdf", "[Source: biology.pdf, page 1]\nCells have membranes.", app.utc_now()),
        )
        document_id = connection.execute("SELECT id FROM documents WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()["id"]
        connection.execute(
            "INSERT INTO conversation_documents (conversation_id, document_id, created_at) VALUES (?, ?, ?)",
            (conversation_id, document_id, app.utc_now()),
        )
    monkeypatch.setattr(app, "semantic_scores", lambda query, chunks: [0.0] * len(chunks))
    monkeypatch.setattr(app, "ai_answer", lambda prompt, context="", memory="": "grounded answer")
    response = first.post(
        "/api/rag/chat",
        data={"message": "What do cells have?", "conversation_id": conversation_id},
        headers=csrf,
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == conversation_id
    assert len(first.get(f"/api/chat?conversation_id={conversation_id}").json()["messages"]) == 2
    assert second.get(f"/api/chat?conversation_id={conversation_id}").status_code == 404


def test_wrong_quiz_answers_create_deduplicated_review_cards() -> None:
    review_client = TestClient(app.app)
    register_verified(review_client, "adaptive@example.com", "+919876543226")
    questions = [{"question": "Which option is correct?", "options": ["A", "B", "C", "D"], "answer": 2, "explanation": "Option C is correct."}]
    with app.db() as connection:
        user_id = connection.execute("SELECT id FROM users WHERE identifier = ?", ("adaptive@example.com",)).fetchone()["id"]
        quiz_id = connection.execute(
            "INSERT INTO quizzes (user_id, title, questions_json, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "Adaptive quiz", json.dumps(questions), app.utc_now()),
        ).lastrowid
    csrf = {"X-CSRF-Token": review_client.cookies["csrf_token"]}
    first = review_client.post(f"/api/quiz/{quiz_id}/submit", data={"answers": json.dumps([0])}, headers=csrf)
    second = review_client.post(f"/api/quiz/{quiz_id}/submit", data={"answers": json.dumps([0])}, headers=csrf)
    assert first.json()["review_cards_created"] == 1
    assert second.json()["review_cards_created"] == 0
    assert len(review_client.get("/api/reviews").json()["reviews"]) == 1
    progress = review_client.get("/api/learning/progress").json()
    assert progress["topics"][0]["topic"] == "Document study"


def test_review_cards_can_be_completed_and_rescheduled() -> None:
    review_client = TestClient(app.app)
    register_verified(review_client, "review-complete@example.com", "+919876543228")
    csrf = {"X-CSRF-Token": review_client.cookies["csrf_token"]}
    created = review_client.post("/api/reviews", data={"prompt": "What is a cell?", "answer": "The basic unit of life."}, headers=csrf)
    review_id = created.json()["id"]
    completed = review_client.post(f"/api/reviews/{review_id}/complete", data={"remembered": "true"}, headers=csrf)
    assert completed.status_code == 200
    assert completed.json()["interval_days"] == 1
    repeated = review_client.post(f"/api/reviews/{review_id}/complete", data={"remembered": "false"}, headers=csrf)
    assert repeated.status_code == 200
    assert repeated.json()["interval_days"] == 1


def test_invalid_conversation_id_is_not_silently_rerouted() -> None:
    conversation_client = TestClient(app.app)
    register_verified(conversation_client, "invalid-conversation@example.com", "+919876543229")
    response = conversation_client.get("/api/chat?conversation_id=999999")
    assert response.status_code == 404


def test_first_login_onboarding_is_per_user_and_persisted() -> None:
    onboarding_client = TestClient(app.app)
    register_verified(onboarding_client, "onboarding@example.com", "+919876543227")
    first_dashboard = onboarding_client.get("/dashboard")
    assert first_dashboard.status_code == 200
    assert "welcome-tour" in first_dashboard.text
    assert "Hear the introduction" in first_dashboard.text
    completed = onboarding_client.post(
        "/api/onboarding/complete",
        headers={"X-CSRF-Token": onboarding_client.cookies["csrf_token"]},
    )
    assert completed.json() == {"completed": True}
    returning_dashboard = onboarding_client.get("/dashboard")
    assert 'class="welcome-tour"' in returning_dashboard.text
    assert 'class="welcome-tour" hidden' in returning_dashboard.text


def test_onboarding_completion_requires_authentication() -> None:
    response = client.post(
        "/api/onboarding/complete",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token", "")},
    )
    assert response.status_code == 401
