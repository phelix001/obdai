#!/usr/bin/env python3
"""SDK-free Claude / OpenAI clients — stdlib only, no pydantic.

Why this exists: the official `anthropic` / `openai` SDKs depend on pydantic v2,
which pulls **pydantic-core** — a Rust extension that Chaquopy cannot package for
Android (chaquo/chaquopy#1017 is open with no prebuilt wheel). FastAPI has the same
dependency. So the on-device build can't use them.

These engines speak the same REST APIs using only `urllib` from the standard
library, and expose the *same duck-typed surface* the chat loop already uses —
`engine.client.messages.create(...)` returning objects with `.content` blocks
(`.type` / `.text` / `.name` / `.input` / `.id` / `.model_dump()`) and
`.stop_reason`, plus `engine.structured(prompt, schema)`. That means
`obd_chat.run_turn` and `obd_diagnose.final_diagnosis` work unchanged.

Desktop keeps using the SDKs (nicer retries/streaming); Android uses these.
`obd_diagnose.build_engine` falls back here automatically when an SDK is absent.
"""

import json
import os
import urllib.error
import urllib.request

ANTHROPIC_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
OPENAI_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com") + "/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"


class ProviderError(RuntimeError):
    """An HTTP call to the model provider failed — message is user-facing."""


def _post_json(url, payload, headers, timeout=120):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:500]
        except Exception:
            pass
        raise ProviderError(f"{e.code} from {url}: {body or e.reason}") from e
    except urllib.error.URLError as e:
        raise ProviderError(f"could not reach {url}: {e.reason}") from e


# --------------------------------------------------------------------------- #
# Duck-typed response objects (mirror the SDK shapes the chat loop reads)
# --------------------------------------------------------------------------- #
class _Block:
    """A content block: .type/.text or .id/.name/.input, plus .model_dump()."""

    def __init__(self, d):
        object.__setattr__(self, "_d", dict(d))

    def __getattr__(self, key):
        try:
            return self._d[key]
        except KeyError:
            raise AttributeError(key) from None

    def model_dump(self):
        return dict(self._d)

    def __repr__(self):
        return f"_Block({self._d!r})"


class _Message:
    def __init__(self, d):
        self._d = d
        self.content = [_Block(b) for b in d.get("content", [])]
        self.stop_reason = d.get("stop_reason")

    def model_dump(self):
        return dict(self._d)


class _Function:
    def __init__(self, d):
        self.name = d.get("name")
        self.arguments = d.get("arguments") or "{}"


class _ToolCall:
    def __init__(self, d):
        self.id = d.get("id")
        self.type = d.get("type", "function")
        self.function = _Function(d.get("function") or {})


class _ChatMessage:
    def __init__(self, d):
        self._d = d
        self.content = d.get("content")
        self.tool_calls = [_ToolCall(t) for t in (d.get("tool_calls") or [])] or None

    def model_dump(self, exclude_none=False):
        out = dict(self._d)
        if exclude_none:
            out = {k: v for k, v in out.items() if v is not None}
        return out


class _Choice:
    def __init__(self, d):
        self.message = _ChatMessage(d.get("message") or {})


class _Completion:
    def __init__(self, d):
        self.choices = [_Choice(c) for c in d.get("choices", [])]


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
class _AnthropicMessages:
    def __init__(self, api_key):
        self._key = api_key

    def create(self, model, max_tokens=1500, messages=None, system=None, tools=None,
               thinking=None, output_config=None, **_ignored):
        payload = {"model": model, "max_tokens": max_tokens, "messages": messages or []}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        if output_config:
            payload["output_config"] = output_config
        # `thinking` is intentionally dropped: the callers only ever disable it,
        # and omitting the field is the REST equivalent of disabled.
        return _Message(_post_json(ANTHROPIC_URL, payload, {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
        }))


class _AnthropicClient:
    def __init__(self, api_key):
        self.messages = _AnthropicMessages(api_key)


class ClaudeHttpEngine:
    """Claude over plain HTTPS — same interface as obd_diagnose.ClaudeEngine."""
    name = "Claude"

    def __init__(self, api_key, model="claude-sonnet-5"):
        if not api_key:
            raise ProviderError("no Anthropic API key")
        self.client = _AnthropicClient(api_key)
        self.model = model

    def structured(self, prompt, schema):
        msg = self.client.messages.create(
            model=self.model, max_tokens=2048,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        return json.loads(text)


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
class _OpenAICompletions:
    def __init__(self, api_key):
        self._key = api_key

    def create(self, model, messages, tools=None, response_format=None, **_ignored):
        payload = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        return _Completion(_post_json(OPENAI_URL, payload,
                                      {"authorization": f"Bearer {self._key}"}))


class _OpenAIChat:
    def __init__(self, api_key):
        self.completions = _OpenAICompletions(api_key)


class _OpenAIClient:
    def __init__(self, api_key):
        self.chat = _OpenAIChat(api_key)


class OpenAIHttpEngine:
    """OpenAI over plain HTTPS — same interface as obd_diagnose.OpenAIEngine."""
    name = "OpenAI"

    def __init__(self, api_key, model=None):
        if not api_key:
            raise ProviderError("no OpenAI API key")
        self.client = _OpenAIClient(api_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")

    def structured(self, prompt, schema):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "result", "schema": schema, "strict": True}})
        return json.loads(resp.choices[0].message.content)
