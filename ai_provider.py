from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Gemini 3.6 Flash is the default text/grounding model. The native voice
# assistant uses the separate Live API model configured in app.py.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
logger = logging.getLogger("study_buddy.ai")


def answer(prompt: str, context: str = "") -> str:
    system = "You are a warm, concise study tutor. Explain clearly, ask guiding questions, and never invent facts. When learner material includes [Source: filename, page N] markers, cite the relevant source and page in the answer. If the material does not contain the answer, say so plainly."
    if context:
        prompt = f"Use this learner material when relevant:\n{context}\n\nQuestion:\n{prompt}"
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        payload = json.dumps({"system_instruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1_200}}).encode()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gemini_key}"
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode())
                return body["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as error:
            logger.warning("Gemini text request failed: %s", type(error).__name__)
            return "Gemini could not answer right now. Your message has still been saved; please check the Gemini key and model configuration."

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return "No AI provider is configured yet. Add GEMINI_API_KEY to the local environment, then I can tutor you. Your message has still been saved."
    payload = json.dumps({"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 1_200}).encode()
    request = urllib.request.Request(GROQ_URL, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {groq_key}"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode())
            return body.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or "I could not generate an answer yet."
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
        logger.warning("Groq text request failed: %s", type(error).__name__)
        return "The configured Groq tutor could not answer right now. Check the Groq key, model, or network connection. Your message has still been saved."
