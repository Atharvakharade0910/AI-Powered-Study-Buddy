import json

import ai_provider


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_gemini_provider_contract(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse({"candidates": [{"content": {"parts": [{"text": "  A clear answer.  "}]}}]})

    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", fake_urlopen)
    assert ai_provider.answer("Why is the sky blue?", "light scattering") == "A clear answer."
    assert "generateContent" in captured["url"]
    assert captured["payload"]["contents"][0]["parts"][0]["text"].startswith("<learner_material>")
    assert captured["timeout"] == 90


def test_provider_missing_key_is_explicit(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    response = ai_provider.answer("What is photosynthesis?")
    assert response.startswith("No AI provider is configured")
