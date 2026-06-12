"""Unit test for the live thinking-token counter in LLMClient.stream_chat.

Reasoning deltas are counted (one delta ≈ one token) and emitted as throttled
``thinking_progress`` events — never as text. Redis / app context not needed.
"""
import json

import app.llm as llmmod
from app.llm import LLMClient


class FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for line in self._lines:
            yield line


def _sse(obj):
    return ("data: " + json.dumps(obj)).encode()


def test_stream_chat_throttles_thinking_progress(monkeypatch):
    # 50 reasoning deltas, then one answer token, then usage.
    lines = [_sse({"choices": [{"delta": {"reasoning_content": "x"}}]}) for _ in range(50)]
    lines.append(_sse({"choices": [{"delta": {"content": "Hi"}}]}))
    lines.append(_sse({"usage": {"prompt_tokens": 3, "completion_tokens": 1}}))
    lines.append(b"data: [DONE]")

    # Disable the time-based emit and pin the token cadence so only the
    # every-16-tokens rule fires (deterministic, immune to prod tuning).
    monkeypatch.setattr(llmmod, "_THINK_EMIT_EVERY_SECONDS", 9999)
    monkeypatch.setattr(llmmod, "_THINK_EMIT_EVERY_TOKENS", 16)
    monkeypatch.setattr(llmmod.requests, "post", lambda *a, **k: FakeResp(lines))

    client = LLMClient("http://x", "m", "sys")
    out = list(client.stream_chat([{"role": "user", "content": "hi"}]))

    starts = [o for o in out if isinstance(o, dict) and "thinking_start" in o]
    progress = [o for o in out if isinstance(o, dict) and "thinking_progress" in o]
    ends = [o for o in out if isinstance(o, dict) and "thinking_end" in o]

    assert len(starts) == 1
    # 50 deltas throttled to every 16 -> emitted at counts 16, 32, 48 (not 50 events).
    assert [p["thinking_progress"] for p in progress] == [16, 32, 48]
    assert len(ends) == 1 and ends[0]["tokens"] == 50
    # No reasoning text ever leaks into the yielded content.
    assert "".join(o for o in out if isinstance(o, str)) == "Hi"
