"""Phase D: the local gateway against a fake upstream (no network)."""

import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from budgetpilot.core import cc_ledger, hookdb, pricebook, state
from budgetpilot.gateway.server import GatewayConfig, gateway_billing, make_server
from budgetpilot.gateway.usage_parse import UsageReader
from budgetpilot.hosts import get_adapter
from tests.hookutil import TRANSCRIPT

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "gateway" / "claude_code_2.1.328_capture.json").read_text())
SECRET = "sk-ant-api03-GATEWAY-TEST-SECRET-0123456789"
PROMPT = "SECRET-PROMPT-TEXT-never-store-me"
OAUTH_BETA = "oauth-2025-04-20,interleaved-thinking-2025-05-14"


def _sse(events):
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)


def anthropic_stream(msg_id="msg_TEST_1", text="ok"):
    """The event sequence captured from Claude Code 2.1.328 (tests/fixtures/gateway), with captured usage numbers."""
    return [
        ("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 20741, "cache_read_input_tokens": 0,
                      "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 20741},
                      "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("ping", {"type": "ping"}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}),
        ("message_stop", {"type": "message_stop"}),
    ]


class Upstream:
    """A fake model API that records exactly what it received."""

    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        self.hold_after_first_event = False
        self.gzip = False
        self.encoding = None
        self.msg_id = "msg_TEST_1"
        upstream = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                upstream.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                if self.path.startswith("/v1/messages/count_tokens"):
                    return self._json({"input_tokens": 10})
                if self.path.startswith("/v1/messages"):
                    req = json.loads(body)
                    if not req.get("stream"):
                        return self._json({"id": upstream.msg_id, "type": "message", "model": "claude-haiku-5-5",
                                           "content": [{"type": "text", "text": "ok"}],
                                           "usage": {"input_tokens": 7, "cache_read_input_tokens": 100,
                                                     "output_tokens": 3}})
                    return self._stream(anthropic_stream(upstream.msg_id))
                if self.path.startswith("/v1/chat/completions"):
                    return self._json({"id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-test",
                                       "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                                                "prompt_tokens_details": {"cached_tokens": 800},
                                                                "completion_tokens_details": {"reasoning_tokens": 20}}})
                self._json({"error": "not found"}, 404)

            def _json(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, events):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                if upstream.encoding:
                    self.send_header("Content-Encoding", upstream.encoding)
                self.end_headers()
                payload = _sse(events)
                if upstream.gzip:
                    payload = gzip.compress(payload)
                upstream.raw = payload
                first, rest = payload[:200], payload[200:]
                for part in (first, rest):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                    if part is first and upstream.hold_after_first_event:
                        upstream.release.wait(5)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def setup(tmp_path):
    up = Upstream()
    cfg = GatewayConfig(upstreams={"anthropic": up.url, "openai": up.url}, capture_dir=str(tmp_path / "capture"))
    gw = make_server(cfg, port=0)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    port = gw.server_address[1]
    yield up, cfg, port
    gw.shutdown()
    up.server.shutdown()


def _post(port, path, body: bytes, headers=None, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", "Host": f"{host}:{port}"}
    h.update(headers or {})
    conn.request("POST", path, body=body, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def _messages_body(stream=True):
    # Unusual spacing and key order on purpose: the upstream must receive these exact bytes.
    return ('{"model":"claude-haiku-5-5",  "stream": %s, "messages":[{"role":"user","content":[{"type":"text",'
            '"text":"%s","cache_control":{"type":"ephemeral","ttl":"1h"}}]}],"max_tokens":16}'
            % ("true" if stream else "false", PROMPT)).encode()


def _rows(expect=1, timeout=3.0):
    """Ledger rows; waits briefly, since the gateway records usage just after the response ends."""
    deadline = time.time() + timeout
    while True:
        rows = _read_rows()
        if len(rows) >= expect or time.time() > deadline:
            return rows
        time.sleep(0.02)


def _read_rows():
    conn = hookdb.open_ready()
    return [dict(zip(("message_id", "session_id", "input_tokens", "cache_write_1h", "cache_read", "output_tokens",
                      "host", "provider", "source"), r)) for r in conn.execute(
        "SELECT message_id, session_id, input_tokens, cache_write_1h, cache_read, output_tokens, host, provider, "
        "source FROM cc_turns ORDER BY message_id")]


# ---------------------------------------------------------------- passthrough
def test_request_and_response_bytes_pass_through_unchanged(setup):
    up, _, port = setup
    body = _messages_body(stream=True)
    resp, data = _post(port, "/v1/messages?beta=true", body, {"x-api-key": SECRET, "anthropic-version": "2023-06-01",
                                                              "x-claude-code-session-id": "s-pass"})
    assert resp.status == 200
    got = up.requests[-1]
    assert got["body"] == body and got["path"] == "/v1/messages?beta=true"          # byte for byte
    assert got["headers"]["x-api-key"] == SECRET and got["headers"]["anthropic-version"] == "2023-06-01"
    assert data == up.raw                                                            # response unchanged too


def test_gzip_stream_is_relayed_unchanged_and_its_usage_recorded(setup):
    up, _, port = setup
    up.gzip, up.encoding = True, "gzip"
    resp, data = _post(port, "/v1/messages?beta=true", _messages_body(),
                       {"authorization": "Bearer " + SECRET, "anthropic-beta": OAUTH_BETA,
                        "x-claude-code-session-id": "s-gzip"})
    assert resp.getheader("Content-Encoding") == "gzip" and gzip.decompress(data) == gzip.decompress(up.raw)
    row = _rows()[0]
    assert row == {"message_id": "msg_TEST_1", "session_id": "s-gzip", "input_tokens": 2, "cache_write_1h": 20741,
                   "cache_read": 0, "output_tokens": 4, "host": "gateway", "provider": "anthropic", "source": "gateway"}
    billing = hookdb.session_billing(hookdb.open_ready(), "s-gzip")
    assert billing["billing_mode"] == "subscription"                                 # plan login via OAuth beta


def test_the_stream_reaches_the_client_before_the_upstream_finishes(setup):
    up, _, port = setup
    up.hold_after_first_event = True
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/messages", body=_messages_body(), headers={"Host": f"127.0.0.1:{port}",
                                                                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    started = time.time()
    first = resp.read1(65536)
    assert first.startswith(b"event: message_start") and time.time() - started < 3   # not buffered
    up.release.set()
    resp.read()
    conn.close()


def test_non_streaming_and_count_tokens(setup):
    up, _, port = setup
    _post(port, "/v1/messages", _messages_body(stream=False), {"x-claude-code-session-id": "s-json"})
    resp, data = _post(port, "/v1/messages/count_tokens", b'{"model":"m"}', {"x-claude-code-session-id": "s-json"})
    assert json.loads(data) == {"input_tokens": 10}
    rows = _rows()
    assert len(rows) == 1 and (rows[0]["input_tokens"], rows[0]["cache_read"], rows[0]["output_tokens"]) == (7, 100, 3)


def test_openai_chat_usage_is_normalised(setup):
    _, _, port = setup
    _post(port, "/v1/chat/completions", b'{"model":"gpt-test","messages":[]}', {"authorization": "Bearer x"})
    row = _rows()[0]
    assert (row["input_tokens"], row["cache_read"], row["output_tokens"], row["provider"]) == (200, 800, 50, "openai")


# ---------------------------------------------------------------- privacy
def test_no_secret_or_prompt_text_is_stored_anywhere(setup, tmp_path):
    up, cfg, port = setup
    _post(port, "/v1/messages", _messages_body(), {"x-api-key": SECRET, "x-claude-code-session-id": "s-priv"})
    _post(port, "/v1/messages", _messages_body(stream=False), {"authorization": "Bearer " + SECRET})
    _rows(expect=2)
    for root in (state.get_bp_home(), Path(cfg.capture_dir)):
        files = [p for p in root.rglob("*") if p.is_file()]
        assert files
        for path in files:
            data = path.read_bytes()
            assert SECRET.encode() not in data and PROMPT.encode() not in data, path
    record = json.loads(sorted(Path(cfg.capture_dir).glob("*.json"))[0].read_text())
    assert record["request_headers"]["x-api-key"] == "<present>"
    assert record["request_body_keys"]["cache_control_markers"] == 1


def test_only_local_requests_are_served(setup):
    up, _, port = setup
    resp, _ = _post(port, "/v1/messages", _messages_body(), host="evil.example.com")
    assert resp.status == 403 and not up.requests


def test_unreachable_upstream_is_a_502(tmp_path):
    cfg = GatewayConfig(upstreams={"anthropic": "http://127.0.0.1:9"})
    gw = make_server(cfg, port=0)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    resp, data = _post(gw.server_address[1], "/v1/messages", _messages_body())
    gw.shutdown()
    assert resp.status == 502 and json.loads(data)["error"]["type"] == "api_error"


def test_unreadable_encoding_passes_through_without_a_usage_row(setup):
    up, cfg, port = setup
    up.encoding = "br"                                       # no brotli decoder in the standard library
    resp, data = _post(port, "/v1/messages", _messages_body())
    assert resp.status == 200 and data == up.raw and _rows(timeout=0.5) == []
    deadline = time.time() + 3
    while cfg.stats["unparsed"] != 1 and time.time() < deadline:
        time.sleep(0.02)
    assert cfg.stats["unparsed"] == 1


# ---------------------------------------------------------------- hard budget
def test_hard_budget_refuses_model_calls_but_not_token_counts(setup):
    up, cfg, port = setup
    cfg.hard_budget_units = 100
    headers = {"x-claude-code-session-id": "s-budget"}
    first, _ = _post(port, "/v1/messages", _messages_body(), headers)            # 2 + 2*20741 + 5*4 units
    _rows(expect=1)
    calls = len(up.requests)
    second, data = _post(port, "/v1/messages", _messages_body(), headers)
    assert first.status == 200 and second.status == 400 and len(up.requests) == calls   # not forwarded
    error = json.loads(data)
    assert error["type"] == "error" and "hard budget" in error["error"]["message"]
    counted, _ = _post(port, "/v1/messages/count_tokens", b'{"model":"m"}', headers)
    assert counted.status == 200 and cfg.stats["blocked"] == 1
    other, _ = _post(port, "/v1/messages", _messages_body(), {"x-claude-code-session-id": "s-other"})
    assert other.status == 200                                                     # per session


def test_hard_budget_is_off_by_default(setup):
    _, cfg, port = setup
    assert cfg.hard_budget_units is None
    for _ in range(3):
        assert _post(port, "/v1/messages", _messages_body(), {"x-claude-code-session-id": "s-free"})[0].status == 200


# ---------------------------------------------------------------- ledger and evidence
def test_a_response_seen_by_gateway_and_transcript_is_counted_once(setup):
    up, _, port = setup
    first_id = next(r["message"]["id"] for r in map(json.loads, TRANSCRIPT.read_text().splitlines())
                    if r.get("type") == "assistant")
    up.msg_id = first_id
    _post(port, "/v1/messages", _messages_body(), {"x-claude-code-session-id": "s1"})
    _rows(expect=1)
    conn = hookdb.open_ready()
    cc_ledger.ingest(conn, "s1", str(TRANSCRIPT))
    assert conn.execute("SELECT COUNT(*) FROM cc_turns WHERE session_id='s1'").fetchone()[0] == 12


def test_captured_traffic_reproduces_claude_codes_own_cost():
    """The two model calls the gateway saw cost exactly what Claude Code reported for the session."""
    meta = CAPTURE["_meta"]["claude_code_reported"]
    total = 0.0
    for r in CAPTURE["requests"]:
        f = r["usage"]
        tokens = {"input_tokens": f["input_tokens"], "cache_write_5m": f["cache_write_5m"],
                  "cache_write_1h": f["cache_write_1h"], "cache_read": f["cache_read"],
                  "output_tokens": f["output_tokens"]}
        total += pricebook.units_and_usd(pricebook.price_for("anthropic", r["model"]), tokens)[1]
    assert total == pytest.approx(meta["total_cost_usd"], rel=1e-9)
    main = CAPTURE["requests"][-1]["usage"]
    assert (main["input_tokens"], main["cache_write_1h"], main["output_tokens"]) == (2, 20741, 4)


def test_captured_request_shapes():
    for r in CAPTURE["requests"]:
        assert r["request_headers"]["authorization"] == "<present>" and "x-api-key" not in r["request_headers"]
        assert r["request_headers"]["x-claude-code-session-id"] == "SESSION-ID"
        assert r["sse_events"][0] == "message_start" and "message_delta" in r["sse_events"]
        names = list(r["request_headers"])
        assert gateway_billing("https://api.anthropic.com", names, r["request_headers"]["anthropic-beta"])["mode"] \
            == "subscription"


@pytest.mark.parametrize("upstream,names,beta,mode,provider", [
    ("https://api.anthropic.com", ["x-api-key"], "", "api", "anthropic"),
    ("https://api.anthropic.com", ["authorization"], OAUTH_BETA, "subscription", "anthropic"),
    ("https://api.anthropic.com", ["authorization"], "", "unknown", "anthropic"),
    ("https://api.openai.com", ["authorization"], "", "api", "openai"),
    ("https://bedrock-runtime.us-east-1.amazonaws.com", [], "", "cloud", "bedrock"),
])
def test_gateway_billing(upstream, names, beta, mode, provider):
    b = gateway_billing(upstream, names, beta)
    assert (b["mode"], b["provider"]) == (mode, provider)
    assert get_adapter("gateway").detect_billing({}, {"upstream": upstream, "header_names": names, "beta": beta}).mode == mode


def test_gateway_adapter_declares_a_hard_cap_and_no_tool_control():
    caps = get_adapter("gateway").capabilities()
    assert caps.tier == "gateway" and caps.hard_token_cap and caps.usage_exact
    assert not (caps.intercept_tool_calls or caps.rewrite_tool_input or caps.deny_tool_calls)


# ---------------------------------------------------------------- parser units
def test_openai_responses_stream_usage():
    reader = UsageReader("openai_responses", "text/event-stream")
    reader.feed(_sse([("response.created", {"type": "response.created", "response": {"id": "resp_1", "model": "m"}}),
                      ("response.completed", {"type": "response.completed", "response": {
                          "id": "resp_1", "model": "m", "usage": {"input_tokens": 500, "output_tokens": 40,
                                                                   "input_tokens_details": {"cached_tokens": 300},
                                                                   "output_tokens_details": {"reasoning_tokens": 30}}}})]))
    r = reader.result()
    assert r["id"] == "resp_1" and r["fields"]["cache_read"] == 300 and r["fields"]["reasoning_tokens"] == 30


def test_openai_chat_stream_without_usage_records_nothing():
    reader = UsageReader("openai_chat", "text/event-stream")
    reader.feed(b'data: {"id":"c1","model":"m","choices":[]}\n\ndata: [DONE]\n\n')
    assert reader.result() is None                                    # include_usage was not requested


def test_stream_split_at_any_byte_boundary():
    payload = _sse(anthropic_stream())
    for cut in (1, 7, 50, 333, len(payload) - 3):
        reader = UsageReader("anthropic_messages", "text/event-stream")
        reader.feed(payload[:cut])
        reader.feed(payload[cut:])
        assert reader.result()["fields"]["output_tokens"] == 4
