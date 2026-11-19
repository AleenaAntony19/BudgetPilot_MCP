"""
Usage extraction from model API responses passing through the gateway.

Only numbers, ids and the model name are taken; text is never kept.

Front doors (by request path):
    /v1/messages             Anthropic Messages API (JSON or server-sent events)
    /v1/chat/completions     OpenAI-compatible chat completions [VERIFY with captured traffic]
    /v1/responses            OpenAI Responses API [VERIFY with captured traffic]

Anthropic shapes are confirmed from traffic captured through the gateway
(tests/fixtures/gateway). OpenAI shapes follow OpenAI's API reference:
chat `usage` {prompt_tokens, completion_tokens, prompt_tokens_details.cached_tokens,
completion_tokens_details.reasoning_tokens}; streaming chat sends usage in a
final chunk only when the request sets stream_options.include_usage;
Responses `usage` {input_tokens, input_tokens_details.cached_tokens,
output_tokens, output_tokens_details.reasoning_tokens}, streamed in the
`response.completed` event.

Standard library only.
"""

from __future__ import annotations

import json
import zlib

ANTHROPIC = {"cached_is_subset_of_input": False, "reasoning_is_subset_of_output": True}
OPENAI = {"cached_is_subset_of_input": True, "reasoning_is_subset_of_output": True}

KINDS = {
    "anthropic_messages": ("anthropic", ANTHROPIC),
    "openai_chat": ("openai", OPENAI),
    "openai_responses": ("openai", OPENAI),
}


def kind_for_path(path: str):
    """Which front door a request path is (None = pass through without usage or budget)."""
    bare = path.split("?", 1)[0].rstrip("/")
    if bare.endswith("/v1/messages"):
        return "anthropic_messages"
    if bare.endswith("/v1/chat/completions"):
        return "openai_chat"
    if bare.endswith("/v1/responses"):
        return "openai_responses"
    return None


def _num(v) -> int:
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def anthropic_fields(usage: dict) -> dict:
    """Raw counts from an Anthropic `usage` object (cache writes split into 5m/1h when reported)."""
    creation = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write = _num(usage.get("cache_creation_input_tokens"))
    w1 = _num(creation.get("ephemeral_1h_input_tokens"))
    w5 = _num(creation.get("ephemeral_5m_input_tokens"))
    if w1 + w5 != write:
        w5, w1 = max(0, write - w1), w1
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {"input_tokens": _num(usage.get("input_tokens")), "cache_write_5m": w5, "cache_write_1h": w1,
            "cache_read": _num(usage.get("cache_read_input_tokens")), "output_tokens": _num(usage.get("output_tokens")),
            "reasoning_tokens": _num(details.get("thinking_tokens"))}


def openai_fields(usage: dict) -> dict:
    """Raw counts from an OpenAI chat or Responses `usage` object (cached and reasoning are subsets)."""
    if "prompt_tokens" in usage:
        inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        idet, odet = usage.get("prompt_tokens_details"), usage.get("completion_tokens_details")
    else:
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        idet, odet = usage.get("input_tokens_details"), usage.get("output_tokens_details")
    idet = idet if isinstance(idet, dict) else {}
    odet = odet if isinstance(odet, dict) else {}
    return {"input_tokens": _num(inp), "cache_write_5m": 0, "cache_write_1h": 0,
            "cache_read": _num(idet.get("cached_tokens")), "output_tokens": _num(out),
            "reasoning_tokens": _num(odet.get("reasoning_tokens"))}


class UsageReader:
    """
    Fed the response body as it streams past (a copy; the client gets the
    original bytes). `result()` gives {id, model, fields, events} or None.
    """

    MAX_JSON = 20_000_000

    def __init__(self, kind: str, content_type: str = "", content_encoding: str = ""):
        self.kind = kind
        self.sse = "text/event-stream" in (content_type or "").lower()
        enc = (content_encoding or "").lower().strip()
        self.decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else
                        zlib.decompressobj() if enc == "deflate" else None)
        self.unreadable = enc not in ("", "identity", "gzip", "deflate")   # e.g. br: no stdlib decoder
        self.buffer = b""
        self.body = bytearray()
        self.id = self.model = None
        self.usage = {}
        self.events = []

    def feed(self, chunk: bytes) -> None:
        if self.unreadable or not chunk:
            return
        try:
            data = self.decoder.decompress(chunk) if self.decoder else chunk
        except zlib.error:
            self.unreadable = True
            return
        if not self.sse:
            if len(self.body) + len(data) <= self.MAX_JSON:
                self.body += data
            return
        self.buffer += data
        while True:
            cut = min((i for i in (self.buffer.find(b"\n\n"), self.buffer.find(b"\r\n\r\n")) if i >= 0), default=-1)
            if cut < 0:
                break
            block, self.buffer = self.buffer[:cut], self.buffer[cut:].lstrip(b"\r\n")
            self._event(block)

    def _event(self, block: bytes) -> None:
        name, data = None, []
        for line in block.decode("utf-8", errors="replace").splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        payload = "\n".join(data)
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        name = name or (obj.get("type") if isinstance(obj, dict) else None)
        if name and name not in self.events:
            self.events.append(name)
        self._take(obj, name)

    def _take(self, obj, name) -> None:
        if not isinstance(obj, dict):
            return
        if self.kind == "anthropic_messages":
            message = obj.get("message") if name == "message_start" else obj if name is None else None
            if isinstance(message, dict):
                self.id = message.get("id") or self.id
                self.model = message.get("model") or self.model
                if isinstance(message.get("usage"), dict):
                    self.usage.update({k: v for k, v in message["usage"].items() if v is not None})
            if name == "message_delta" and isinstance(obj.get("usage"), dict):
                # message_delta carries cumulative usage; its non-null fields win.
                self.usage.update({k: v for k, v in obj["usage"].items() if v is not None})
        elif self.kind == "openai_responses":
            response = obj.get("response") if name and name.startswith("response.") else obj
            if isinstance(response, dict):
                self.id = response.get("id") or self.id
                self.model = response.get("model") or self.model
                if isinstance(response.get("usage"), dict):
                    self.usage = dict(response["usage"])
        else:  # openai_chat
            self.id = obj.get("id") or self.id
            self.model = obj.get("model") or self.model
            if isinstance(obj.get("usage"), dict):
                self.usage = dict(obj["usage"])

    def result(self):
        if self.unreadable:
            return None
        if not self.sse and self.body:
            try:
                self._take(json.loads(bytes(self.body)), None)
            except ValueError:
                return None
        elif self.sse and self.buffer.strip():
            self._event(self.buffer)
            self.buffer = b""
        if not self.id or not self.usage:
            return None
        fields = anthropic_fields(self.usage) if self.kind == "anthropic_messages" else openai_fields(self.usage)
        return {"id": str(self.id), "model": self.model, "fields": fields, "events": list(self.events)}
