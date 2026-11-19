"""
`bp gateway`: a local HTTP proxy between a coding agent and a model API.

    agent --(ANTHROPIC_BASE_URL=http://127.0.0.1:8899)--> gateway --> https://api.anthropic.com

What it does
- Binds 127.0.0.1 only and refuses requests whose Host header is not local.
- Forwards every request unchanged: same method, path, headers (except
  hop-by-hop headers and Host) and the exact body bytes. Earlier messages are
  never reordered or rewritten, so `cache_control` and prompt caching work as
  without the gateway.
- Streams the response back as it arrives (server-sent events included) and
  reads token usage from a copy: the final usage of a streamed message, or the
  `usage` of a JSON response. Each response is stored once, keyed by its id
  (an Anthropic response id equals the transcript's message id, so a session
  seen by both the hooks and the gateway is not double counted).
- Optional hard budget (default off): once a session has used its budget, a
  model call gets an error response instead of being forwarded. This is the
  one place BudgetPilot can cap spend; hooks only act at tool boundaries.

What it never does
- Store or log prompts, completions, or the values of authentication headers.
  Stored: token counts, model, timestamps, the response id, a session key.
- Make requests on its own.

Standard library only.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse, parse_qsl

from budgetpilot.gateway.usage_parse import KINDS, UsageReader, kind_for_path

DEFAULT_PORT = 8899
DEFAULT_UPSTREAMS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization", "x-goog-api-key",
                  "api-key", "openai-organization", "openai-project"}
# Headers whose values are recorded in capture mode: protocol metadata, never credentials.
SAFE_VALUE_HEADERS = {"anthropic-version", "anthropic-beta", "user-agent", "x-app", "content-type", "accept",
                      "accept-encoding", "content-encoding", "request-id", "x-request-id", "anthropic-ratelimit-unified-status"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class GatewayConfig:
    def __init__(self, upstreams=None, hard_budget_units=None, session_headers=None, capture_dir=None):
        self.upstreams = dict(DEFAULT_UPSTREAMS, **(upstreams or {}))
        self.hard_budget_units = float(hard_budget_units) if hard_budget_units else None
        # Request headers that identify an agent session, first match wins. Claude Code sends
        # x-claude-code-session-id (captured on 2.1.328); other hosts: [VERIFY].
        self.session_headers = [h.lower() for h in (session_headers or ["x-claude-code-session-id"])]
        self.capture_dir = capture_dir
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.lock = threading.Lock()
        self.stats = {"requests": 0, "recorded": 0, "unparsed": 0, "blocked": 0, "upstream_errors": 0}

    @classmethod
    def from_config(cls, cfg: dict, **overrides):
        section = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
        merged = {"upstreams": section.get("upstreams"), "hard_budget_units": section.get("hard_budget_units"),
                  "session_headers": section.get("session_headers"), "capture_dir": None}
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**merged)


def upstream_provider(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("anthropic.com"):
        return "anthropic"
    if host.endswith("openai.com"):
        return "openai"
    if "bedrock" in host:
        return "bedrock"
    if host.endswith("googleapis.com"):
        return "vertex"
    if host.endswith("azure.com"):
        return "azure"
    return "other"


def gateway_billing(upstream_url: str, header_names, beta: str = "", api_provider: str = "anthropic") -> dict:
    """
    Billing for a gateway request, from the upstream host, which auth header
    is present (names only) and the `anthropic-beta` header (a protocol value,
    not a secret). Captured from Claude Code 2.1.328 with a plan login:
    `authorization` present, no `x-api-key`, and `oauth-2025-04-20` in
    anthropic-beta (tests/fixtures/gateway). A key header means per-token API
    billing; a cloud host means cloud billing.
    """
    provider = upstream_provider(upstream_url)
    if provider == "other":
        provider = api_provider  # a custom upstream (another proxy): the API format says whose API it is
    names = {h.lower() for h in header_names}
    if provider == "anthropic" and "x-api-key" not in names and "authorization" in names \
            and any(part.strip().startswith("oauth-") for part in (beta or "").split(",")):
        return {"mode": "subscription", "provider": "anthropic", "confidence": "high",
                "evidence": ["gateway upstream: anthropic", "plan login (OAuth beta header, no API key header)"]}
    if provider in ("bedrock", "vertex", "azure"):
        return {"mode": "cloud", "provider": provider, "confidence": "high",
                "evidence": [f"gateway upstream: {provider}"]}
    if "x-api-key" in names or ("authorization" in names and provider == "openai"):
        return {"mode": "api", "provider": provider, "confidence": "high",
                "evidence": [f"gateway upstream: {provider}", "request has an API key header"]}
    return {"mode": "unknown", "provider": provider, "confidence": "low",
            "evidence": [f"gateway upstream: {provider}", "request has no API key header"]}


def _session_key(cfg: GatewayConfig, headers) -> str:
    for name in cfg.session_headers:
        value = headers.get(name)
        if value:
            return str(value)[:128]
    return f"gateway-{cfg.started}"


def _error_body(kind: str, message: str) -> bytes:
    if kind and kind.startswith("openai"):
        return json.dumps({"error": {"message": message, "type": "budget_exceeded", "code": "budget_exceeded"}}).encode()
    return json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BudgetPilotGateway"
    cfg: GatewayConfig = None

    def log_message(self, *args):  # no access log: paths and headers can carry identifiers
        pass

    # every method goes through the same passthrough
    def do_GET(self):
        self._proxy()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    # -------------------------------------------------------------- request
    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_plain(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self) -> None:
        cfg = self.cfg
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOCAL_HOSTS:
            self._send_plain(403, b'{"error":"BudgetPilot gateway accepts local requests only"}')
            return
        with cfg.lock:
            cfg.stats["requests"] += 1
        body = self._read_body()
        kind = kind_for_path(self.path)
        provider = KINDS[kind][0] if kind else "anthropic"
        upstream = cfg.upstreams.get(provider) or cfg.upstreams["anthropic"]
        session = _session_key(cfg, self.headers)
        capture = {"ts": time.time(), "method": self.command, "path": self._safe_path(), "kind": kind,
                   "request_headers": self._header_record(self.headers.items()),
                   "request_body_keys": self._body_keys(body)} if cfg.capture_dir else None

        if kind and cfg.hard_budget_units:
            used = _units_used(session)
            if used >= cfg.hard_budget_units:
                with cfg.lock:
                    cfg.stats["blocked"] += 1
                message = (f"BudgetPilot gateway: this session has used {used:,.0f} of its hard budget of "
                           f"{cfg.hard_budget_units:,.0f} billing units, so this request was not sent. Raise "
                           f"gateway.hard_budget_units or start a new session.")
                self._send_plain(400, _error_body(kind, message))
                if capture is not None:
                    capture.update(status=400, blocked=True)
                    _write_capture(cfg, capture)
                return

        target = urlparse(upstream)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        out_headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP]
        out_headers.append(("Host", target.netloc))
        if body or self.command in ("POST", "PUT", "PATCH"):
            out_headers.append(("Content-Length", str(len(body))))
        base = (target.path or "").rstrip("/")
        try:
            conn = conn_cls(target.hostname, target.port, timeout=600)
            conn.putrequest(self.command, base + self.path, skip_host=True, skip_accept_encoding=True)
            for k, v in out_headers:
                conn.putheader(k, v)
            conn.endheaders(body if body else None)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            with cfg.lock:
                cfg.stats["upstream_errors"] += 1
            self._send_plain(502, json.dumps({"type": "error", "error": {
                "type": "api_error", "message": f"BudgetPilot gateway: upstream unreachable ({type(exc).__name__})"}}
            ).encode())
            return

        reader = UsageReader(kind, resp.getheader("Content-Type", ""), resp.getheader("Content-Encoding", "")) \
            if kind and resp.status < 400 else None
        self.send_response_only(resp.status, resp.reason)
        passthrough = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP_BY_HOP]
        for k, v in passthrough:
            self.send_header(k, v)
        length = resp.getheader("Content-Length")
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        chunked = not no_body and length is None
        if length is not None:
            self.send_header("Content-Length", length)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while not no_body:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if reader is not None:
                    reader.feed(chunk)
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away; usage read so far is still recorded below
        finally:
            conn.close()

        result = reader.result() if reader is not None else None
        if result:
            _record_usage(cfg, kind, session, upstream, result, [k for k, _ in self.headers.items()],
                          self.headers.get("anthropic-beta") or "")
        elif reader is not None:
            with cfg.lock:
                cfg.stats["unparsed"] += 1
        if capture is not None:
            capture.update(status=resp.status, response_headers=self._header_record(passthrough),
                           sse_events=result["events"] if result else (reader.events if reader else []),
                           usage=result["fields"] if result else None, response_id=result["id"] if result else None,
                           model=result["model"] if result else None)
            _write_capture(cfg, capture)

    # -------------------------------------------------------------- capture helpers (no secrets, no text)
    def _safe_path(self) -> str:
        parsed = urlparse(self.path)
        keys = [k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)]
        return parsed.path + (("?" + "&".join(f"{k}=…" for k in keys)) if keys else "")

    @staticmethod
    def _header_record(items) -> dict:
        out = {}
        for k, v in items:
            name = k.lower()
            if name in SECRET_HEADERS:
                out[name] = "<present>"
            elif name in SAFE_VALUE_HEADERS or "session" in name or name.startswith("x-stainless"):
                out[name] = v
            else:
                out[name] = "<value not recorded>"
        return out

    @staticmethod
    def _body_keys(body: bytes):
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        record = {"keys": sorted(obj.keys()), "stream": obj.get("stream"), "model": obj.get("model")}
        # Where cache_control markers sit (counts only): shows that caching instructions pass through.
        record["cache_control_markers"] = json.dumps(obj).count('"cache_control"')
        return record


def _units_used(session: str) -> float:
    from budgetpilot.core import cc_ledger, hookdb

    conn = hookdb.open_ready()
    if conn is None:
        return 0.0
    try:
        return float(cc_ledger.session_totals(conn, session_id=session)["billing_units"] or 0.0)
    finally:
        conn.close()


def _record_usage(cfg: GatewayConfig, kind: str, session: str, upstream: str, result: dict, header_names,
                  beta: str = "") -> None:
    from budgetpilot.core import hookdb
    from budgetpilot.hosts.base import UsageEvent
    from budgetpilot.hosts.usage import store_events

    provider, semantics = KINDS[kind]
    f = result["fields"]
    event = UsageEvent(
        session_id=session, host="gateway", provider=provider, model=result["model"],
        ts=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), message_id=result["id"],
        input_tokens=f["input_tokens"], cache_write_5m=f["cache_write_5m"], cache_write_1h=f["cache_write_1h"],
        cache_read=f["cache_read"], output_tokens=f["output_tokens"], reasoning_tokens=f["reasoning_tokens"],
        semantics=semantics, source="gateway", raw_ref=f"{kind}:{result['id']}",
    )
    conn = hookdb.open_ready()
    if conn is None:
        return
    try:
        hookdb.touch_session(conn, session, "", "", "gateway")
        billing = gateway_billing(upstream, header_names, beta, provider)
        if not hookdb.session_billing(conn, session).get("billing_mode") and billing["mode"] != "unknown":
            hookdb.record_billing(conn, session, {
                "billing_mode": billing["mode"], "billing_provider": billing["provider"],
                "billing_confidence": billing["confidence"], "billing_evidence": json.dumps(billing["evidence"]),
                "billing_source": "detected"})
        # A session the gateway created is a gateway session; one the hooks created stays claude_code.
        conn.execute("UPDATE cc_sessions SET host_adapter='gateway' WHERE session_id=? AND host='gateway'", (session,))
        store_events(conn, [event])
        hookdb.heartbeat(conn, "gateway", "gateway", {"upstreams": sorted(cfg.upstreams)}, min_interval=0)
        with cfg.lock:
            cfg.stats["recorded"] += 1
    except Exception as exc:
        hookdb.log_error("gateway", exc)
    finally:
        conn.close()


def _write_capture(cfg: GatewayConfig, record: dict) -> None:
    try:
        os.makedirs(cfg.capture_dir, exist_ok=True)
        name = f"{cfg.started}-{int(record['ts'] * 1000)}-{record['method']}.json"
        with open(os.path.join(cfg.capture_dir, name), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
    except OSError:
        pass


def make_server(cfg: GatewayConfig, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("Handler", (GatewayHandler,), {"cfg": cfg})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bp gateway", description="Local model API gateway (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--anthropic-upstream", default=None)
    parser.add_argument("--openai-upstream", default=None)
    parser.add_argument("--hard-budget-units", type=float, default=None)
    parser.add_argument("--capture", default=None, help="Directory for redacted request/response shapes")
    args = parser.parse_args(argv)
    return run(args.port, args.anthropic_upstream, args.openai_upstream, args.hard_budget_units, args.capture)


def run(port=None, anthropic_upstream=None, openai_upstream=None, hard_budget_units=None, capture=None,
        out=print) -> int:
    from budgetpilot.core import state

    cfg_file = state.read_config()
    section = cfg_file.get("gateway") if isinstance(cfg_file.get("gateway"), dict) else {}
    upstreams = dict(section.get("upstreams") or {})
    if anthropic_upstream:
        upstreams["anthropic"] = anthropic_upstream
    if openai_upstream:
        upstreams["openai"] = openai_upstream
    cfg = GatewayConfig.from_config(cfg_file, upstreams=upstreams, hard_budget_units=hard_budget_units,
                                    capture_dir=capture)
    port = port or int(section.get("port") or DEFAULT_PORT)
    server = make_server(cfg, port)
    out(f"BudgetPilot gateway on http://127.0.0.1:{port} (local only)")
    for provider, url in cfg.upstreams.items():
        out(f"  {provider:<9} -> {url}")
    out(f"  hard budget: {f'{cfg.hard_budget_units:,.0f} billing units per session' if cfg.hard_budget_units else 'off'}")
    out(f"Point Claude Code at it:  export ANTHROPIC_BASE_URL=http://127.0.0.1:{port}")
    out("Stop with Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        out(f"Gateway stopped. {cfg.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
