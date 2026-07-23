#!/usr/bin/env python3
"""Tests for the SDK-free provider engines (obd_provider) + engine selection.

Spec asserted: the obd_provider docstring contract — these stdlib-only engines expose
the SAME duck-typed surface the chat loop already consumes (`client.messages.create`
returning `.content` blocks with `.type`/`.text`/`.name`/`.input`/`.id`/`.model_dump()`
and `.stop_reason`; OpenAI's `.choices[0].message` with `.content`/`.tool_calls`/
`.model_dump(exclude_none=True)`), plus `structured()`. This is what lets the Android
build drop the anthropic/openai SDKs — they need pydantic-core (Rust), which Chaquopy
cannot package (chaquo/chaquopy#1017).

All network calls are stubbed; these run offline with no API key.

Run:  venv/bin/python -m pytest test_obd_provider.py -q
"""

import json

import pytest

import obd_provider


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
CLAUDE_TOOL_REPLY = {
    "content": [
        {"type": "text", "text": "Let me read the car."},
        {"type": "tool_use", "id": "tu_1", "name": "read_current", "input": {"signals": ["rpm"]}},
    ],
    "stop_reason": "tool_use",
}


def test_claude_blocks_match_the_sdk_surface(monkeypatch):
    sent = {}

    def fake_post(url, payload, headers, timeout=120):
        sent.update(url=url, payload=payload, headers=headers)
        return CLAUDE_TOOL_REPLY

    monkeypatch.setattr(obd_provider, "_post_json", fake_post)
    eng = obd_provider.ClaudeHttpEngine("k")
    r = eng.client.messages.create(model="m", max_tokens=100, system="sys",
                                   tools=[{"name": "read_current"}],
                                   messages=[{"role": "user", "content": "hi"}],
                                   thinking={"type": "disabled"})
    assert r.stop_reason == "tool_use"
    text, tool = r.content
    assert text.type == "text" and text.text == "Let me read the car."
    assert tool.type == "tool_use" and tool.name == "read_current"
    assert tool.input == {"signals": ["rpm"]} and tool.id == "tu_1"
    # session files must stay JSON-serializable
    assert json.dumps([b.model_dump() for b in r.content])


def test_claude_payload_shape_and_auth(monkeypatch):
    sent = {}
    monkeypatch.setattr(obd_provider, "_post_json",
                        lambda url, payload, headers, timeout=120:
                        (sent.update(url=url, payload=payload, headers=headers), CLAUDE_TOOL_REPLY)[1])
    obd_provider.ClaudeHttpEngine("secret").client.messages.create(
        model="claude-sonnet-5", max_tokens=42, system="S", tools=[{"name": "t"}],
        messages=[{"role": "user", "content": "x"}], thinking={"type": "disabled"})
    p = sent["payload"]
    assert p["model"] == "claude-sonnet-5" and p["max_tokens"] == 42
    assert p["system"] == "S" and p["tools"] == [{"name": "t"}]
    assert "thinking" not in p          # dropped: omitting == disabled over REST
    assert sent["headers"]["x-api-key"] == "secret"
    assert sent["headers"]["anthropic-version"] == obd_provider.ANTHROPIC_VERSION
    assert sent["url"].endswith("/v1/messages")


def test_claude_omits_optional_fields(monkeypatch):
    sent = {}
    monkeypatch.setattr(obd_provider, "_post_json",
                        lambda url, payload, headers, timeout=120:
                        (sent.update(payload=payload), CLAUDE_TOOL_REPLY)[1])
    obd_provider.ClaudeHttpEngine("k").client.messages.create(
        model="m", messages=[{"role": "user", "content": "x"}])
    assert "system" not in sent["payload"] and "tools" not in sent["payload"]


def test_claude_structured_parses_json(monkeypatch):
    payload_seen = {}

    def fake_post(url, payload, headers, timeout=120):
        payload_seen.update(payload)
        return {"content": [{"type": "text", "text": '{"most_likely_problem": "PCV leak"}'}],
                "stop_reason": "end_turn"}

    monkeypatch.setattr(obd_provider, "_post_json", fake_post)
    out = obd_provider.ClaudeHttpEngine("k").structured("prompt", {"type": "object"})
    assert out == {"most_likely_problem": "PCV leak"}
    assert payload_seen["output_config"]["format"]["type"] == "json_schema"


def test_claude_requires_a_key():
    with pytest.raises(obd_provider.ProviderError):
        obd_provider.ClaudeHttpEngine("")


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
OPENAI_TOOL_REPLY = {
    "choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "read_vin", "arguments": '{"x":1}'}}],
    }}]
}


def test_openai_message_surface(monkeypatch):
    monkeypatch.setattr(obd_provider, "_post_json",
                        lambda *a, **k: OPENAI_TOOL_REPLY)
    eng = obd_provider.OpenAIHttpEngine("k", model="gpt-4o")
    resp = eng.client.chat.completions.create(model="gpt-4o", messages=[], tools=[])
    msg = resp.choices[0].message
    assert msg.content is None
    assert msg.tool_calls[0].id == "c1"
    assert msg.tool_calls[0].function.name == "read_vin"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"x": 1}
    dumped = msg.model_dump(exclude_none=True)
    assert "content" not in dumped and dumped["role"] == "assistant"


def test_openai_no_tool_calls_is_none(monkeypatch):
    monkeypatch.setattr(obd_provider, "_post_json", lambda *a, **k:
                        {"choices": [{"message": {"role": "assistant", "content": "hi"}}]})
    resp = obd_provider.OpenAIHttpEngine("k").client.chat.completions.create("m", [])
    assert resp.choices[0].message.tool_calls is None
    assert resp.choices[0].message.content == "hi"


def test_openai_auth_header(monkeypatch):
    sent = {}
    monkeypatch.setattr(obd_provider, "_post_json",
                        lambda url, payload, headers, timeout=120:
                        (sent.update(headers=headers, url=url), OPENAI_TOOL_REPLY)[1])
    obd_provider.OpenAIHttpEngine("sk-x").client.chat.completions.create("m", [])
    assert sent["headers"]["authorization"] == "Bearer sk-x"
    assert sent["url"].endswith("/v1/chat/completions")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_http_error_becomes_provider_error(monkeypatch):
    import urllib.error, io

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))

    monkeypatch.setattr(obd_provider.urllib.request, "urlopen", boom)
    with pytest.raises(obd_provider.ProviderError) as e:
        obd_provider._post_json("https://x/y", {}, {})
    assert "401" in str(e.value)


def test_unreachable_becomes_provider_error(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(obd_provider.urllib.request, "urlopen", boom)
    with pytest.raises(obd_provider.ProviderError, match="could not reach"):
        obd_provider._post_json("https://x/y", {}, {})


# --------------------------------------------------------------------------- #
# Engine selection — the Android fallback
# --------------------------------------------------------------------------- #
def test_http_engines_used_when_sdk_absent(monkeypatch):
    import obd_diagnose
    monkeypatch.setattr(obd_diagnose, "anthropic", None)      # simulate Android
    monkeypatch.delenv("OBD_HTTP_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(obd_diagnose.build_engine("claude"), obd_provider.ClaudeHttpEngine)


def test_env_var_forces_http_engine(monkeypatch):
    import obd_diagnose
    monkeypatch.setenv("OBD_HTTP_PROVIDER", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(obd_diagnose.build_engine("claude"), obd_provider.ClaudeHttpEngine)


def test_sdk_used_when_available(monkeypatch):
    import obd_diagnose
    if obd_diagnose.anthropic is None:
        pytest.skip("anthropic SDK not installed in this environment")
    monkeypatch.delenv("OBD_HTTP_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(obd_diagnose.build_engine("claude"), obd_diagnose.ClaudeEngine)
