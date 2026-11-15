"""
BudgetPilot dashboard server.

    python -m budgetpilot.dashboard.server [--port 8765]

- binds 127.0.0.1 only and refuses requests whose Host header is not local
- opens the database read-only (`file:...?mode=ro`); it cannot change anything
- one embedded HTML page, no external requests, no CDN, no CORS headers
- exits after 30 minutes without a request

Every number on the page comes from a SQL query in this file. Token savings
shown under "estimated" are estimates; the only measured figure is the paired
A/B result written by `bp ab`.

Standard library only.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import random
import sqlite3
import sys
import threading
import time
from urllib.parse import urlparse

import budgetpilot
from budgetpilot.core import state

IDLE_EXIT_SECONDS = 30 * 60
PORT_ATTEMPTS = 20
_last_request = time.time()


# ------------------------------------------------------------------ data access
def connect(db_path=None):
    """Read-only connection, or None when the database does not exist yet."""
    path = str(db_path or state.get_bp_home() / "bp.db")
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def rows(conn, sql, params=()):
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def one(conn, sql, params=()):
    found = rows(conn, sql, params)
    return found[0] if found else {}


def _json_field(value, default):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


_TOKEN_SUMS = (
    "COUNT(*) AS turns, COALESCE(SUM(input_tokens),0) AS input, "
    "COALESCE(SUM(cache_write_5m+cache_write_1h),0) AS cache_write, COALESCE(SUM(cache_read),0) AS cache_read, "
    "COALESCE(SUM(output_tokens),0) AS output, COALESCE(SUM(thinking_tokens),0) AS thinking, "
    "COALESCE(SUM(billing_units),0) AS billing_units, COALESCE(SUM(ref_usd),0) AS ref_usd, "
    "COALESCE(MAX(price_guess),0) AS price_guess"
)


def _with_ratio(totals):
    denominator = (totals.get("input") or 0) + (totals.get("cache_write") or 0) + (totals.get("cache_read") or 0)
    totals["cache_hit_ratio"] = round((totals.get("cache_read") or 0) / denominator, 4) if denominator else None
    return totals


def ab_summary(conn):
    """Latest paired A/B experiment: relative billing-unit difference with a bootstrap 95 % CI."""
    latest = one(conn, "SELECT pair_id FROM ab_runs ORDER BY ts DESC LIMIT 1")
    if not latest:
        return {"available": False}
    experiment = str(latest["pair_id"]).split(":")[0]
    runs = rows(conn, "SELECT pair_id, arm, passed, billing_units, turns FROM ab_runs WHERE pair_id LIKE ?",
                (experiment + ":%",))
    pairs = {}
    for run in runs:
        pairs.setdefault(run["pair_id"], {})[run["arm"]] = run
    diffs, observe_pass, enforce_pass, observe_units, enforce_units = [], 0, 0, 0.0, 0.0
    for pair in pairs.values():
        if "observe" in pair and "enforce" in pair and pair["observe"]["billing_units"]:
            base, treated = pair["observe"], pair["enforce"]
            diffs.append((treated["billing_units"] - base["billing_units"]) / base["billing_units"])
            observe_pass += int(bool(base["passed"]))
            enforce_pass += int(bool(treated["passed"]))
            observe_units += base["billing_units"]
            enforce_units += treated["billing_units"]
    if not diffs:
        return {"available": False}
    rng = random.Random(0)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(2000))
    return {
        "available": True, "experiment": experiment, "pairs": len(diffs),
        "mean_change_pct": round(100 * sum(diffs) / len(diffs), 1),
        "ci_low_pct": round(100 * means[int(0.025 * len(means))], 1),
        "ci_high_pct": round(100 * means[int(0.975 * len(means)) - 1], 1),
        "total_change_pct": round(100 * (enforce_units - observe_units) / observe_units, 1) if observe_units else None,
        "observe_pass": observe_pass, "enforce_pass": enforce_pass,
    }


def api_summary(conn):
    now = time.time()
    day_ago_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
    session = one(conn, "SELECT session_id FROM cc_turns ORDER BY ts DESC LIMIT 1").get("session_id")
    series = [
        _with_ratio({"input": r["i"], "cache_write": r["w"], "cache_read": r["r"]})["cache_hit_ratio"] or 0
        for r in reversed(rows(
            conn, "SELECT input_tokens AS i, cache_write_5m+cache_write_1h AS w, cache_read AS r FROM cc_turns "
                  "WHERE session_id=? ORDER BY ts DESC LIMIT 60", (session,)))
    ]
    avoided = one(conn, "SELECT COALESCE(SUM(tokens_avoided_est),0) AS n FROM gate_events WHERE verdict='rewrite'")
    trimmed = one(conn, "SELECT COALESCE(SUM(trimmed_tokens_est),0) AS n FROM tool_obs WHERE truncated=1")
    overhead = one(conn, "SELECT COALESCE(SUM(overhead_tokens_est),0) AS n FROM tasks_bp")
    gross = int(avoided.get("n") or 0) + int(trimmed.get("n") or 0)
    connections = {c["name"]: c for c in rows(conn, "SELECT name, kind, last_seen, detail FROM connections")}
    host = _json_field((connections.get("hooks") or {}).get("detail"), {}).get("host")
    task = one(conn, "SELECT task_id, scope, scope_conf, mode, ctx_used_est, ctx_budget, out_used, out_budget, status, "
                     "pending_recovery, ts FROM tasks_bp ORDER BY ts DESC LIMIT 1")
    return {
        "as_of": time.strftime("%H:%M:%S"), "version": budgetpilot.__version__,
        "mode": state.get_mode(), "budget": state.get_budget_slider(),
        "hooks_last_seen": (connections.get("hooks") or {}).get("last_seen"),
        "mcp_last_seen": (connections.get("mcp") or {}).get("last_seen"),
        "host": host, "session_id": session,
        "tokens_session": _with_ratio(one(conn, f"SELECT {_TOKEN_SUMS} FROM cc_turns WHERE session_id=?", (session,))),
        "tokens_24h": _with_ratio(one(conn, f"SELECT {_TOKEN_SUMS} FROM cc_turns WHERE ts >= ?", (day_ago_iso,))),
        "cache_ratio_series": series,
        "cache_drops": [i for i in range(1, len(series)) if series[i - 1] - series[i] > 0.20],
        "measured": ab_summary(conn),
        "estimated": {"avoided_by_gate": int(avoided.get("n") or 0), "avoided_by_trim": int(trimmed.get("n") or 0),
                      "overhead": int(overhead.get("n") or 0), "net": gross - int(overhead.get("n") or 0)},
        "task": task or None,
    }


def api_tasks(conn):
    return {"tasks": rows(conn, "SELECT task_id, session_id, ts, scope, scope_conf, mode, budget_slider, "
                                "prompt_tokens_est, ctx_used_est, ctx_budget, out_used, out_budget, status, "
                                "overhead_tokens_est, pending_recovery FROM tasks_bp ORDER BY ts DESC LIMIT 25")}


def api_gate(conn):
    return {
        "counts": {r["verdict"]: r["n"] for r in rows(conn, "SELECT verdict, COUNT(*) AS n FROM gate_events GROUP BY verdict")},
        "top_rules": rows(conn, "SELECT rule, verdict, COUNT(*) AS n, COALESCE(SUM(tokens_avoided_est),0) AS avoided_est "
                                "FROM gate_events WHERE verdict NOT IN ('allow') GROUP BY rule, verdict "
                                "ORDER BY n DESC LIMIT 8"),
        "recent": rows(conn, "SELECT ts, tool, verdict, rule, reason, tokens_avoided_est, latency_ms FROM gate_events "
                             "ORDER BY id DESC LIMIT 10"),
        "latency_ms": one(conn, "SELECT ROUND(AVG(latency_ms),2) AS mean, ROUND(MAX(latency_ms),2) AS max FROM gate_events"),
    }


def api_failures(conn):
    found = rows(conn, "SELECT signature, failure_class, count, first_seen, last_seen, sample_message, actions_tried, "
                       "resolved_by FROM failure_fingerprints ORDER BY last_seen DESC LIMIT 25")
    for item in found:
        item["actions_tried"] = _json_field(item["actions_tried"], [])
        item["resolved_by"] = _json_field(item["resolved_by"], {})
    return {"fingerprints": found}


def api_recovery(conn):
    found = rows(conn, "SELECT ts, signature, failure_class, attempt, router_scores, recommended, chosen, decided_by, "
                       "outcome FROM recovery_events ORDER BY id DESC LIMIT 25")
    for item in found:
        item["router_scores"] = _json_field(item["router_scores"], {})
    return {"events": found}


def api_connections(conn):
    found = rows(conn, "SELECT name, kind, last_seen, detail FROM connections ORDER BY name")
    for item in found:
        item["detail"] = _json_field(item["detail"], {})
    session = one(conn, "SELECT session_id, transcript_path, cwd, last_seen FROM cc_sessions ORDER BY last_seen DESC LIMIT 1")
    calibration = {"ratio": None}
    if conn is not None:
        try:
            from budgetpilot.core import cc_ledger

            calibration = cc_ledger.calibration(conn)
        except Exception:
            pass
    return {"connections": found, "transcript": session or None, "calibration": calibration,
            "dashboard": {"pid": os.getpid(), "now": time.time()}}


def api_ab(conn):
    return {"summary": ab_summary(conn),
            "runs": rows(conn, "SELECT pair_id, arm, task_id, repeat, passed, billing_units, in_tok, cache_w, cache_r, "
                               "out_tok, turns, wall_s, ts FROM ab_runs ORDER BY ts DESC LIMIT 60")}


ROUTES = {
    "/api/summary": api_summary, "/api/tasks": api_tasks, "/api/gate": api_gate, "/api/failures": api_failures,
    "/api/recovery": api_recovery, "/api/connections": api_connections, "/api/ab": api_ab,
}

# ------------------------------------------------------------------ page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BudgetPilot</title>
<style>
:root{--bg:#fff;--fg:#1f2a1f;--muted:#5f6b5f;--panel:#e8f5e9;--accent:#2e7d32;--line:#cfe3d1;--warn:#b26a00;--bad:#b3261e}
@media (prefers-color-scheme: dark){:root{--bg:#121512;--fg:#e4ece4;--muted:#9aa89a;--panel:#1b2a1d;--accent:#81c784;--line:#2c3f2e;--warn:#ffb74d;--bad:#ef9a9a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px;color:var(--accent)}h2{font-size:13px;margin:0 0 8px;color:var(--accent);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);font-size:12px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;min-width:0}
.wide{grid-column:1/-1}
.strip{display:flex;flex-wrap:wrap;gap:6px 18px}.strip div{white-space:nowrap}
.k{color:var(--muted)}.v{font-weight:600}.big{font-size:28px;font-weight:700;color:var(--accent)}
.pill{display:inline-block;padding:1px 8px;border:1px solid var(--accent);border-radius:10px;color:var(--accent);font-weight:600}
.pill.off{border-color:var(--muted);color:var(--muted)}.pill.observe{border-color:var(--warn);color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:3px 6px 3px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}td.n,th.n{text-align:right}
.scroll{overflow-x:auto}.empty{color:var(--muted);font-style:italic}
.bar{height:8px;background:var(--bg);border:1px solid var(--line);border-radius:4px;overflow:hidden;margin:2px 0 8px}
.bar i{display:block;height:100%;background:var(--accent)}.bar i.over{background:var(--bad)}
.est{color:var(--muted);font-size:12px}.warn{color:var(--warn)}.bad{color:var(--bad)}
svg{display:block;width:100%;height:36px}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
</style></head><body><main>
<h1>BudgetPilot</h1><div class="sub" id="asof">loading…</div>
<div class="grid">
<section class="wide"><h2>Status</h2><div class="strip" id="status"></div></section>
<section><h2>Tokens</h2><div id="tokens"></div></section>
<section><h2>Savings</h2><div id="savings"></div></section>
<section><h2>Scope &amp; budget</h2><div id="scope"></div></section>
<section><h2>Gate</h2><div id="gate"></div></section>
<section class="wide"><h2>Failure fingerprints (persistent ledger)</h2><div class="scroll" id="failures"></div></section>
<section class="wide"><h2>Recovery</h2><div class="scroll" id="recovery"></div></section>
<section class="wide"><h2>Connections</h2><div class="scroll" id="connections"></div></section>
</div></main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num=n=>n==null?"–":Number(n).toLocaleString("en-US",{maximumFractionDigits:0});
const pct=x=>x==null?"–":(100*x).toFixed(1)+"%";
const ago=t=>{if(!t)return"never";const s=Math.max(0,Date.now()/1000-t);return s<90?Math.round(s)+" s ago":s<5400?Math.round(s/60)+" min ago":s<172800?Math.round(s/3600)+" h ago":Math.round(s/86400)+" d ago"};
const empty=t=>`<div class="empty">${t}</div>`;
const table=(heads,body)=>`<table><tr>${heads.map(h=>`<th${h.n?' class="n"':""}>${h.t??h}</th>`).join("")}</tr>${body}</table>`;
function spark(v,drops){if(!v||v.length<2)return empty("no data yet");const w=300,h=36,x=i=>i*w/(v.length-1),y=p=>h-2-p*(h-4);
 const pts=v.map((p,i)=>x(i).toFixed(1)+","+y(p).toFixed(1)).join(" ");
 const marks=(drops||[]).map(i=>`<circle cx="${x(i).toFixed(1)}" cy="${y(v[i]).toFixed(1)}" r="3" fill="var(--bad)"/>`).join("");
 return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="${pts}"/>${marks}</svg>`}
function tok(t,label){if(!t||!t.turns)return `<div class="k">${label}</div>`+empty("no data yet");
 return `<div class="k">${label} · ${num(t.turns)} turns</div>`+table(["input","cache write","cache read","output","thinking*"].map(t=>({t,n:1})),
 `<tr><td class="n">${num(t.input)}</td><td class="n">${num(t.cache_write)}</td><td class="n">${num(t.cache_read)}</td><td class="n">${num(t.output)}</td><td class="n">${num(t.thinking)}</td></tr>`)+
 `<div>billing units <span class="v">${num(t.billing_units)}</span> · cache-hit <span class="v">${pct(t.cache_hit_ratio)}</span> · API-equivalent $${Number(t.ref_usd).toFixed(4)}${t.price_guess?' <span class="warn">(price guessed)</span>':""}</div>`}
const bar=(u,b)=>{const p=b?Math.min(100,100*u/b):0;return `<div class="bar"><i class="${u>b?"over":""}" style="width:${p}%"></i></div>`};
async function get(p){const r=await fetch(p,{cache:"no-store"});return r.json()}
async function refresh(){try{
 const [s,g,f,r,c]=await Promise.all(["summary","gate","failures","recovery","connections"].map(n=>get("/api/"+n)));
 $("asof").textContent=`as of ${s.as_of} · local only (127.0.0.1) · read-only · refreshes every 3 s`;
 const label={enforce:"ON",observe:"OBSERVE",off:"OFF"}[s.mode]||s.mode;
 $("status").innerHTML=`<div><span class="pill ${esc(s.mode)}">${label}</span></div><div><span class="k">budget</span> <span class="v">${esc(s.budget)}</span></div>
 <div><span class="k">hooks</span> <span class="v">${ago(s.hooks_last_seen)}</span></div><div><span class="k">MCP</span> <span class="v">${ago(s.mcp_last_seen)}</span></div>
 <div><span class="k">host</span> <span class="v">${esc(s.host||"–")}</span></div><div><span class="k">version</span> <span class="v">${esc(s.version)}</span></div>`;
 $("tokens").innerHTML=tok(s.tokens_session,"This session")+"<br>"+tok(s.tokens_24h,"Last 24 hours")+
 `<div class="k" style="margin-top:8px">cache-hit ratio per turn${s.cache_drops.length?' · <span class="bad">'+s.cache_drops.length+" drop(s) over 20 points</span>":""}</div>`+spark(s.cache_ratio_series,s.cache_drops)+
 `<div class="est">*thinking tokens are already included in output. API-equivalent cost: a Pro/Max plan is not billed per token.</div>`;
 const m=s.measured,e=s.estimated;
 $("savings").innerHTML=`<div class="k">Measured (paired A/B)</div>`+(m.available?
 `<div class="big">${m.mean_change_pct>0?"+":""}${m.mean_change_pct}%</div><div>billing units, enforce vs observe · 95% CI ${m.ci_low_pct}% to ${m.ci_high_pct}% · ${m.pairs} pairs</div>
 <div>tasks passed: observe ${m.observe_pass}/${m.pairs}, enforce ${m.enforce_pass}/${m.pairs}</div>`:
 `<div class="big">–</div><div>not run yet — <code>bp ab smoke</code></div>`)+
 `<div class="est" style="margin-top:10px">Estimated (not measured): avoided by gate ~${num(e.avoided_by_gate)} tokens, by trimming ~${num(e.avoided_by_trim)}; BudgetPilot overhead ~${num(e.overhead)}; net ~${num(e.net)} (estimated).</div>`;
 const t=s.task;
 $("scope").innerHTML=t?`<div>scope <span class="v">${t.scope}</span> <span class="k">(confidence ${Number(t.scope_conf).toFixed(2)}, ${esc(t.mode)}, ${esc(t.status)})</span></div>
 <div class="k">context added by tools: ~${num(t.ctx_used_est)} / ${num(t.ctx_budget)} (estimated)</div>${bar(t.ctx_used_est,t.ctx_budget)}
 <div class="k">output: ${num(t.out_used)} / ${num(t.out_budget)}</div>${bar(t.out_used,t.out_budget)}
 ${t.pending_recovery?`<div class="warn">recovery pending: ${esc(t.pending_recovery.slice(0,8))}</div>`:""}`:empty("no task yet");
 const counts=Object.entries(g.counts);
 $("gate").innerHTML=counts.length?`<div class="strip">${counts.map(([k,v])=>`<div><span class="k">${esc(k)}</span> <span class="v">${num(v)}</span></div>`).join("")}</div>`+
 (g.top_rules.length?table(["rule","verdict",{t:"n",n:1},{t:"avoided (est.)",n:1}],g.top_rules.map(x=>`<tr><td>${esc(x.rule)}</td><td>${esc(x.verdict)}</td><td class="n">${num(x.n)}</td><td class="n">${num(x.avoided_est)}</td></tr>`).join("")):"")+
 `<div class="k" style="margin-top:8px">last 10</div>`+table(["tool","verdict","rule"],g.recent.map(x=>`<tr><td>${esc(x.tool)}</td><td>${esc(x.verdict)}</td><td title="${esc(x.reason)}">${esc(x.rule)}</td></tr>`).join("")):empty("no data yet");
 $("failures").innerHTML=f.fingerprints.length?table(["signature","class",{t:"count",n:1},"last seen","actions tried","resolved by","message"],
 f.fingerprints.map(x=>`<tr><td><code>${esc(x.signature.slice(0,8))}</code></td><td>${esc(x.failure_class)}</td><td class="n">${num(x.count)}</td><td>${ago(x.last_seen)}</td><td>${esc(x.actions_tried.join(", ")||"–")}</td><td>${esc(Object.entries(x.resolved_by).map(([k,v])=>k+" ×"+v).join(", ")||"–")}</td><td>${esc(x.sample_message)}</td></tr>`).join("")):empty("no failures recorded yet");
 $("recovery").innerHTML=r.events.length?table(["when","signature","class",{t:"attempt",n:1},"recommended","chosen","by","outcome"],
 r.events.map(x=>`<tr><td>${ago(x.ts)}</td><td><code>${esc((x.signature||"").slice(0,8))}</code></td><td>${esc(x.failure_class)}</td><td class="n">${x.attempt}</td><td>${esc(x.recommended)}</td><td>${esc(x.chosen||"–")}</td><td>${esc(x.decided_by||"–")}</td><td>${esc(x.outcome||"–")}</td></tr>`).join("")):empty("no recovery decisions yet");
 const cal=c.calibration;
 $("connections").innerHTML=(c.connections.length?table(["name","kind","last seen","detail"],c.connections.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.kind)}</td><td>${ago(x.last_seen)}</td><td>${esc(Object.entries(x.detail).filter(([k,v])=>v!=null).map(([k,v])=>k+"="+v).join(" "))}</td></tr>`).join("")):empty("no connections yet — run <code>bp install</code>, then start a Claude Code session"))+
 `<div style="margin-top:8px"><span class="k">dashboard</span> pid ${c.dashboard.pid} · <span class="k">transcript</span> ${esc(c.transcript?c.transcript.transcript_path:"none yet")}</div>
 <div><span class="k">estimator calibration (derived)</span> ${cal.ratio==null?"no data yet":`estimated / derived-actual = ${cal.ratio} over ${cal.tasks} task(s)`+(cal.ok?"":' <span class="warn">outside ±25%</span>')}</div>`;
}catch(err){$("asof").textContent="dashboard server not reachable"}}
refresh();setInterval(refresh,3000);
</script></body></html>
"""


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "BudgetPilotDashboard"
    db_path = None

    def log_message(self, *args):  # silence the default stderr access log
        pass

    def _send(self, status, body, content_type):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global _last_request
        _last_request = time.time()
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        route = ROUTES.get(path)
        if route is None:
            self._send(404, '{"error":"not found"}', "application/json")
            return
        conn = connect(self.db_path)
        try:
            payload = route(conn)
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            if conn is not None:
                conn.close()
        self._send(200, json.dumps(payload), "application/json")


def make_server(port=8765, db_path=None):
    """Bind 127.0.0.1 on `port`, or the next free port."""
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    last_error = None
    for candidate in range(port, port + PORT_ATTEMPTS):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError as exc:
            last_error = exc
    raise OSError(f"no free port in {port}-{port + PORT_ATTEMPTS - 1}: {last_error}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="budgetpilot-dashboard")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.port)
    pidfile = state.get_bp_home() / "dashboard.pid"
    pidfile.write_text(json.dumps({"pid": os.getpid(), "port": server.server_address[1]}), encoding="utf-8")

    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - _last_request > IDLE_EXIT_SECONDS:
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        from budgetpilot.core import hookdb

        conn = hookdb.open_ready()
        if conn is not None:
            hookdb.heartbeat(conn, "dashboard", "dashboard", {"port": server.server_address[1]}, min_interval=0)
            conn.close()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if json.loads(pidfile.read_text()).get("pid") == os.getpid():
                pidfile.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
