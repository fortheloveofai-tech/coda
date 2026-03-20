#!/usr/bin/env python3
"""
Coda MCP Server
Preference-first personalization for Claude and AI apps.

Zero dependencies — runs with python3 stdlib only.

Usage:
  python3 server.py          # MCP stdio server
  python3 server.py --console # Inspection console on http://localhost:3456
"""

import sys
import json
import sqlite3
import threading
import os
import math
import uuid
import re
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request as URLRequest
from urllib.error import URLError
from urllib.parse import urlparse, parse_qs

# ── Config ──────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("CODA_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "coda.db")
CONSOLE_PORT = int(os.environ.get("CODA_CONSOLE_PORT", "3456"))

# ── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

_db_lock = threading.Lock()
_db = get_db()

def db_exec(sql, params=()):
    with _db_lock:
        _db.execute(sql, params)
        _db.commit()

def db_query(sql, params=()):
    with _db_lock:
        return [dict(r) for r in _db.execute(sql, params).fetchall()]

def db_one(sql, params=()):
    with _db_lock:
        row = _db.execute(sql, params).fetchone()
        return dict(row) if row else None

def init_db():
    with _db_lock:
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS preferences (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.7,
                task_scope TEXT,
                pinned INTEGER DEFAULT 0,
                source TEXT DEFAULT 'inferred',
                times_accepted INTEGER DEFAULT 0,
                times_rejected INTEGER DEFAULT 0,
                times_edited INTEGER DEFAULT 0,
                last_reinforced TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_prefs_user ON preferences(user_id);

            CREATE TABLE IF NOT EXISTS feedback_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                retrieval_id TEXT,
                signal TEXT NOT NULL,
                task_type TEXT,
                preference_ids TEXT,
                edit_delta TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_events_user ON feedback_events(user_id);

            CREATE TABLE IF NOT EXISTS retrieval_log (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                task_type TEXT,
                context_hint TEXT,
                preferences_returned TEXT,
                score_breakdown TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_retrieval_user ON retrieval_log(user_id);
        """)
        _db.commit()

def parse_pref(row):
    """Convert a raw DB row dict into a clean preference dict."""
    r = dict(row)
    r["task_scope"] = json.loads(r["task_scope"]) if r.get("task_scope") else None
    r["pinned"] = bool(r.get("pinned", 0))
    return r

def make_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ── Scoring ─────────────────────────────────────────────────────────────────

SIGNAL_WEIGHTS = {"accepted": +0.05, "reused": +0.10, "edited": -0.03, "rejected": -0.10}
COUNTER_MAP    = {"accepted": "times_accepted", "reused": "times_accepted",
                  "edited": "times_edited", "rejected": "times_rejected"}

def task_match_weight(task_scope, task_type):
    if not task_scope:
        return 0.4  # general preference
    if task_type in task_scope:
        return 1.0
    return 0.0

def recency_score(last_reinforced):
    if not last_reinforced:
        return 0.3
    try:
        ts = datetime.fromisoformat(last_reinforced.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        return max(0.1, math.exp(-days / 14))
    except Exception:
        return 0.3

def score_pref(pref, task_type):
    tm = task_match_weight(pref["task_scope"], task_type)
    if tm == 0:
        return 0.0
    total = pref["times_accepted"] + pref["times_rejected"] + pref["times_edited"]
    ar = (pref["times_accepted"] / total) if total > 0 else 0.5
    rs = recency_score(pref.get("last_reinforced"))
    return (tm * 0.4) + (ar * 0.3) + (rs * 0.2) + (pref["confidence"] * 0.1)

def score_breakdown(pref, task_type):
    tm = task_match_weight(pref["task_scope"], task_type)
    total = pref["times_accepted"] + pref["times_rejected"] + pref["times_edited"]
    ar = (pref["times_accepted"] / total) if total > 0 else 0.5
    rs = recency_score(pref.get("last_reinforced"))
    return {
        "preference_id":  pref["id"],
        "value":          pref["value"],
        "task_match":     round(tm, 3),
        "acceptance_rate": round(ar, 3),
        "recency_score":  round(rs, 3),
        "confidence":     round(pref["confidence"], 3),
        "final_score":    round(score_pref(pref, task_type), 3),
    }

def apply_signal(confidence, signal):
    delta = SIGNAL_WEIGHTS.get(signal, 0)
    return max(0.1, min(1.0, confidence + delta))

# ── Tools ───────────────────────────────────────────────────────────────────

def tool_get_preferences(args):
    user_id    = args.get("user_id", "local")
    task_type  = args.get("task_type", "general")
    context    = args.get("context_hint")
    limit      = min(int(args.get("limit", 5)), 20)

    rows = db_query("SELECT * FROM preferences WHERE user_id = ?", (user_id,))
    prefs = [parse_pref(r) for r in rows]

    scored = [(p, score_pref(p, task_type)) for p in prefs]
    scored = [(p, s) for p, s in scored if s > 0]
    scored.sort(key=lambda x: (not x[0]["pinned"], -x[1]))

    top = scored[:limit]
    breakdowns = [score_breakdown(p, task_type) for p, _ in top]

    retrieval_id = make_id("ret")
    db_exec(
        "INSERT INTO retrieval_log (id, user_id, task_type, context_hint, preferences_returned, score_breakdown) VALUES (?,?,?,?,?,?)",
        (retrieval_id, user_id, task_type, context,
         json.dumps([p["id"] for p, _ in top]),
         json.dumps(breakdowns))
    )

    # Summary
    from collections import Counter
    cats = Counter(p["category"] for p, _ in top)
    summary = (f"Returned {len(top)} preferences: " +
               ", ".join(f"{v} {k}" for k, v in cats.items()) +
               ". Scored by task match + recency + acceptance rate + confidence."
               ) if top else "No preferences found yet. They will be learned over time."

    return {
        "preferences": [{
            "id":             p["id"],
            "category":       p["category"],
            "value":          p["value"],
            "confidence":     p["confidence"],
            "task_scope":     p["task_scope"],
            "times_accepted": p["times_accepted"],
            "last_reinforced": p.get("last_reinforced"),
            "pinned":         p["pinned"],
        } for p, _ in top],
        "retrieval_id":      retrieval_id,
        "retrieval_summary": summary,
    }

def tool_log_feedback(args):
    user_id      = args.get("user_id", "local")
    retrieval_id = args.get("retrieval_id")
    signal       = args.get("signal", "accepted")
    task_type    = args.get("task_type")
    pref_ids     = args.get("preference_ids_in_play", [])
    edit_delta   = args.get("edit_delta")

    event_id = make_id("evt")
    db_exec(
        "INSERT INTO feedback_events (id, user_id, retrieval_id, signal, task_type, preference_ids, edit_delta) VALUES (?,?,?,?,?,?,?)",
        (event_id, user_id, retrieval_id, signal, task_type, json.dumps(pref_ids), edit_delta)
    )

    updated = []
    for pref_id in pref_ids:
        row = db_one("SELECT * FROM preferences WHERE id = ? AND user_id = ?", (pref_id, user_id))
        if not row or row.get("pinned"):
            continue
        new_conf = apply_signal(row["confidence"], signal)
        counter  = COUNTER_MAP.get(signal, "times_accepted")
        db_exec(
            f"UPDATE preferences SET confidence=?, {counter}={counter}+1, last_reinforced=datetime('now') WHERE id=?",
            (new_conf, pref_id)
        )
        updated.append({
            "id":               pref_id,
            "confidence_delta": round(new_conf - row["confidence"], 3),
            "new_confidence":   round(new_conf, 3),
        })

    return {"success": True, "event_id": event_id, "preferences_updated": updated}

def tool_upsert_preference(args):
    user_id    = args.get("user_id", "local")
    category   = args.get("category")
    value      = args.get("value", "").strip()
    task_scope = args.get("task_scope")
    confidence = float(args.get("confidence", 0.9))
    pinned     = bool(args.get("pinned", False))
    source     = args.get("source", "explicit")

    if not category or not value:
        return {"error": "category and value are required"}

    scope_json = json.dumps(task_scope) if task_scope else None

    existing = db_one(
        "SELECT * FROM preferences WHERE user_id=? AND category=? AND value=?",
        (user_id, category, value)
    )

    if existing:
        new_conf = max(existing["confidence"], confidence)
        db_exec(
            "UPDATE preferences SET confidence=?, task_scope=COALESCE(?,task_scope), pinned=CASE WHEN ? THEN 1 ELSE pinned END, source=?, last_reinforced=datetime('now') WHERE id=?",
            (new_conf, scope_json, 1 if pinned else 0, source, existing["id"])
        )
        row = db_one("SELECT * FROM preferences WHERE id=?", (existing["id"],))
        return {"preference": parse_pref(row), "action": "updated"}

    pref_id = make_id("pref")
    db_exec(
        "INSERT INTO preferences (id, user_id, category, value, confidence, task_scope, pinned, source) VALUES (?,?,?,?,?,?,?,?)",
        (pref_id, user_id, category, value, confidence, scope_json, 1 if pinned else 0, source)
    )
    row = db_one("SELECT * FROM preferences WHERE id=?", (pref_id,))
    return {"preference": parse_pref(row), "action": "created"}

def tool_explain_retrieval(args):
    retrieval_id = args.get("retrieval_id", "")
    log = db_one("SELECT * FROM retrieval_log WHERE id=?", (retrieval_id,))
    if not log:
        return {"error": f'Retrieval "{retrieval_id}" not found.'}

    returned_ids = set(json.loads(log["preferences_returned"] or "[]"))
    breakdown    = json.loads(log["score_breakdown"] or "[]")
    all_prefs    = [parse_pref(r) for r in db_query("SELECT * FROM preferences WHERE user_id=?", (log["user_id"],))]

    excluded = []
    for p in all_prefs:
        if p["id"] in returned_ids:
            continue
        s = score_pref(p, log["task_type"] or "general")
        if s == 0 and p["task_scope"] and log["task_type"] not in p["task_scope"]:
            reason = f"task_scope mismatch — scoped to [{', '.join(p['task_scope'])}] only"
        elif s == 0:
            reason = "score was 0 for this task type"
        else:
            reason = f"ranked below top {len(returned_ids)} (score: {s:.3f})"
        excluded.append({"preference_id": p["id"], "value": p["value"], "reason": reason})

    return {
        "retrieval_id":           retrieval_id,
        "task_type":              log["task_type"],
        "context_hint":           log["context_hint"],
        "created_at":             log["created_at"],
        "preferences_considered": len(all_prefs),
        "preferences_returned":   len(returned_ids),
        "score_breakdown":        breakdown,
        "excluded_preferences":   excluded,
    }

def tool_import_from_mem0(args):
    user_id      = args.get("user_id", "local")
    mem0_user_id = args.get("mem0_user_id", "")
    mem0_api_key = args.get("mem0_api_key", "")
    dry_run      = bool(args.get("dry_run", False))

    CLASSIFIERS = [
        (re.compile(r'\b(tone|direct|formal|casual|friendly|professional|concise|brief|verbose|jargon)\b', re.I), 'tone', ['writing']),
        (re.compile(r'\b(bullet|format|markdown|table|heading|structure|layout|numbered|bold)\b', re.I), 'format', None),
        (re.compile(r'\b(goal|objective|aim|trying to|want to|building|working on)\b', re.I), 'goals', None),
        (re.compile(r"\b(don'?t|avoid|never|constraint|limit|restrict|must not|no more than)\b", re.I), 'constraints', None),
        (re.compile(r'\b(always|usually|typically|prefer|habit|tend to|every time)\b', re.I), 'habits', None),
        (re.compile(r'\b(engineer|developer|designer|manager|analyst|role|title|senior|junior|lead|founder)\b', re.I), 'role', None),
    ]

    try:
        req = URLRequest(
            f"https://api.mem0.ai/v1/memories/?user_id={mem0_user_id}",
            headers={"Authorization": f"Token {mem0_api_key}", "Content-Type": "application/json"}
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        memories = data if isinstance(data, list) else data.get("results", [])
    except URLError as e:
        return {"error": f"Failed to fetch from Mem0: {e}"}

    created, skipped, preview = 0, 0, []
    for mem in memories:
        text = mem.get("memory", "").strip()
        if not text:
            skipped += 1
            continue
        category = scope = None
        for pattern, cat, sc in CLASSIFIERS:
            if pattern.search(text):
                category, scope = cat, sc
                break
        if not category:
            skipped += 1
            continue

        mapped = {"category": category, "value": text, "confidence": 0.65,
                  "task_scope": scope, "source": "imported"}
        preview.append({"mem0_memory": text, "mapped_to": mapped})

        if not dry_run:
            pref_id = make_id("pref")
            try:
                db_exec(
                    "INSERT OR IGNORE INTO preferences (id, user_id, category, value, confidence, task_scope, source) VALUES (?,?,?,?,?,?,?)",
                    (pref_id, user_id, category, text, 0.65,
                     json.dumps(scope) if scope else None, "imported")
                )
                created += 1
            except Exception:
                skipped += 1
        else:
            created += 1

    return {
        "memories_fetched":    len(memories),
        "preferences_created": created,
        "preferences_skipped": skipped,
        "dry_run":             dry_run,
        "preview":             preview[:5],
    }

# ── MCP Protocol (LSP-style stdio framing) ──────────────────────────────────

TOOLS = [
    {
        "name": "coda_get_preferences",
        "description": (
            "Retrieve the ranked preference slice most relevant to the current task. "
            "Call this at the start of any writing, coding, planning, or support response. "
            "Returns structured preference objects and a retrieval_id for later feedback logging."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id":      {"type": "string", "default": "local"},
                "task_type":    {"type": "string", "enum": ["writing","coding","planning","support","general"]},
                "context_hint": {"type": "string"},
                "limit":        {"type": "number", "default": 5},
            },
            "required": ["task_type"],
        },
    },
    {
        "name": "coda_log_feedback",
        "description": (
            "Log a behavioral signal after delivering a response. "
            "Call when the user accepts, edits, rejects, or re-requests an output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id":                {"type": "string", "default": "local"},
                "retrieval_id":           {"type": "string"},
                "signal":                 {"type": "string", "enum": ["accepted","edited","rejected","reused"]},
                "task_type":              {"type": "string"},
                "preference_ids_in_play": {"type": "array", "items": {"type": "string"}, "default": []},
                "edit_delta":             {"type": "string"},
            },
            "required": ["signal"],
        },
    },
    {
        "name": "coda_upsert_preference",
        "description": (
            "Create or update a preference object. "
            "Call when the user explicitly states a preference ('always use bullet points', 'I am a senior engineer')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id":    {"type": "string", "default": "local"},
                "category":   {"type": "string", "enum": ["tone","format","goals","constraints","habits","role"]},
                "value":      {"type": "string"},
                "task_scope": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "default": 0.9},
                "pinned":     {"type": "boolean", "default": False},
                "source":     {"type": "string", "enum": ["explicit","inferred","imported"], "default": "explicit"},
            },
            "required": ["category", "value"],
        },
    },
    {
        "name": "coda_explain_retrieval",
        "description": "Explain why specific preferences were (or were not) returned for a given retrieval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "retrieval_id": {"type": "string"},
            },
            "required": ["retrieval_id"],
        },
    },
    {
        "name": "coda_import_from_mem0",
        "description": "Import and structure raw Mem0 memories into Coda preference objects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id":      {"type": "string", "default": "local"},
                "mem0_user_id": {"type": "string"},
                "mem0_api_key": {"type": "string"},
                "dry_run":      {"type": "boolean", "default": False},
            },
            "required": ["mem0_user_id", "mem0_api_key"],
        },
    },
]

TOOL_MAP = {
    "coda_get_preferences":   tool_get_preferences,
    "coda_log_feedback":      tool_log_feedback,
    "coda_upsert_preference": tool_upsert_preference,
    "coda_explain_retrieval": tool_explain_retrieval,
    "coda_import_from_mem0":  tool_import_from_mem0,
}

def send_message(msg: dict):
    body = json.dumps(msg)
    header = f"Content-Length: {len(body.encode())}\r\n\r\n"
    data = header.encode() + body.encode()
    # Write directly to fd 1 to bypass all Python buffering layers.
    # sys.stdout.buffer.flush() is unreliable when stdout is a pipe.
    import os
    os.write(1, data)

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode().strip()
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body)

def handle_message(msg: dict):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "coda-mcp", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "notifications/initialized":
        return None  # no response needed

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        fn = TOOL_MAP.get(tool_name)
        if not fn:
            return err(-32601, f"Unknown tool: {tool_name}")
        try:
            result = fn(tool_args)
            return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})

    # Unknown method — return a standard error
    if msg_id is not None:
        return err(-32601, f"Method not found: {method}")
    return None

def run_mcp_server():
    print("Coda MCP server starting...", file=sys.stderr)
    init_db()
    print(f"Database: {DB_PATH}", file=sys.stderr)
    while True:
        msg = read_message()
        if msg is None:
            break
        response = handle_message(msg)
        if response is not None:
            send_message(response)

# ── Inspection Console (HTTP) ───────────────────────────────────────────────

CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Coda — Inspection Console</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;--muted:#64748b;--accent:#4a9eed;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--radius:8px}
    body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
    header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 24px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
    .logo{font-size:18px;font-weight:700}.logo span{color:var(--accent)}
    .badge{background:var(--border);border-radius:20px;padding:4px 12px;font-size:13px;color:var(--muted)}
    main{max-width:1100px;margin:0 auto;padding:24px}
    .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
    .stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
    .stat-val{font-size:28px;font-weight:700}.stat-label{font-size:12px;color:var(--muted);margin-top:2px}
    .sec{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px}
    .filters{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
    .tab{border:1px solid var(--border);border-radius:20px;padding:4px 14px;font-size:12px;cursor:pointer;color:var(--muted);background:transparent;transition:all .15s}
    .tab.active{border-color:var(--accent);color:var(--accent);background:rgba(74,158,237,.08)}
    .prefs{display:flex;flex-direction:column;gap:8px;margin-bottom:32px}
    .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;display:flex;align-items:flex-start;gap:12px;transition:border-color .15s}
    .card:hover{border-color:var(--accent)}.card.pinned{border-left:3px solid var(--amber)}
    .cat{border-radius:4px;padding:3px 8px;font-size:11px;font-weight:600;white-space:nowrap;flex-shrink:0;text-transform:uppercase;letter-spacing:.5px}
    .cat-tone{background:#1e3a5f;color:#93c5fd}.cat-format{background:#1a4d2e;color:#86efac}
    .cat-goals{background:#2d1b69;color:#c4b5fd}.cat-constraints{background:#5c1a1a;color:#fca5a5}
    .cat-habits{background:#3b2f1a;color:#fcd34d}.cat-role{background:#1a3a4d;color:#67e8f9}
    .body{flex:1;min-width:0}.val{font-size:14px;line-height:1.5}
    .meta{display:flex;gap:16px;margin-top:6px;flex-wrap:wrap}
    .meta span{font-size:12px;color:var(--muted)}.meta .cf{color:var(--text);font-weight:600}
    .acts{display:flex;gap:6px;flex-shrink:0;align-items:center}
    .btn{border:1px solid var(--border);background:transparent;color:var(--text);border-radius:5px;padding:4px 10px;font-size:12px;cursor:pointer;transition:all .15s}
    .btn:hover{background:var(--border)}.btn.danger:hover{border-color:var(--red);color:var(--red)}
    .btn.ok{border-color:var(--accent);color:var(--accent)}.btn.ok:hover{background:var(--accent);color:#fff}
    .pin{font-size:14px;cursor:pointer;opacity:.4;transition:opacity .15s}.pin.on{opacity:1;color:var(--amber)}
    .bar-w{width:60px;height:4px;background:var(--border);border-radius:2px;display:inline-block;vertical-align:middle;margin-right:4px}
    .bar{height:100%;border-radius:2px}
    .scope{background:var(--border);border-radius:3px;padding:1px 6px;font-size:11px;color:var(--muted)}
    .empty{text-align:center;color:var(--muted);padding:48px 0;font-size:14px}
    .edit-input{background:var(--bg);border:1px solid var(--accent);border-radius:5px;color:var(--text);font-size:14px;padding:6px 10px;width:100%;font-family:inherit;resize:none}
    .edit-input:focus{outline:none}
    .rl{display:flex;flex-direction:column;gap:8px}
    .ri{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 16px}
    .rh{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
    .rt{font-size:12px;font-weight:600;color:var(--accent);text-transform:uppercase}
    .rtime{font-size:12px;color:var(--muted)}.rp{font-size:12px;color:var(--muted)}
    .rid{font-size:11px;color:var(--border);font-family:monospace;margin-top:2px}
  </style>
</head>
<body>
<header>
  <div class="logo"><span>co</span>da <span style="color:var(--muted);font-weight:400;font-size:14px">inspection console</span></div>
  <div class="badge" id="ulabel">user: local</div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div class="stat-val" id="sp">—</div><div class="stat-label">Preferences learned</div></div>
    <div class="stat"><div class="stat-val" id="sr">—</div><div class="stat-label">Retrievals</div></div>
    <div class="stat"><div class="stat-val" id="se">—</div><div class="stat-label">Feedback events</div></div>
    <div class="stat"><div class="stat-val" id="sa">—</div><div class="stat-label">Acceptance rate</div></div>
  </div>
  <div class="sec">Preference Objects</div>
  <div class="filters" id="filters">
    <button class="tab active" data-cat="all">All</button>
    <button class="tab" data-cat="tone">Tone</button>
    <button class="tab" data-cat="format">Format</button>
    <button class="tab" data-cat="goals">Goals</button>
    <button class="tab" data-cat="constraints">Constraints</button>
    <button class="tab" data-cat="habits">Habits</button>
    <button class="tab" data-cat="role">Role</button>
  </div>
  <div class="prefs" id="prefs"><div class="empty">Loading...</div></div>
  <div class="sec" style="margin-top:32px">Recent Retrievals</div>
  <div class="rl" id="retrs"><div class="empty">Loading...</div></div>
</main>
<script>
const UID = new URLSearchParams(location.search).get('user')||'local';
document.getElementById('ulabel').textContent='user: '+UID;
let all=[], filt='all';
const cc=c=>c>=.8?'#22c55e':c>=.6?'#f59e0b':'#ef4444';
function rel(iso){const d=new Date(iso.endsWith('Z')?iso:iso+'Z'),s=(Date.now()-d)/1000;return s<60?'just now':s<3600?Math.floor(s/60)+'m ago':s<86400?Math.floor(s/3600)+'h ago':Math.floor(s/86400)+'d ago';}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function stats(){const s=await fetch('/api/stats?user_id='+UID).then(r=>r.json());document.getElementById('sp').textContent=s.preferences;document.getElementById('sr').textContent=s.retrievals;document.getElementById('se').textContent=s.feedback_events;document.getElementById('sa').textContent=s.acceptance_rate!=null?s.acceptance_rate+'%':'—';}
async function prefs(){all=await fetch('/api/preferences?user_id='+UID).then(r=>r.json());render();}
function render(){const f=filt==='all'?all:all.filter(p=>p.category===filt);const c=document.getElementById('prefs');if(!f.length){c.innerHTML='<div class="empty">No preferences yet. Start using Coda with Claude to build them up.</div>';return;}c.innerHTML=f.map(p=>{const sc=p.task_scope?p.task_scope.map(s=>'<span class="scope">'+s+'</span>').join(' '):'<span class="scope">all tasks</span>';const bw=Math.round(p.confidence*60);return`<div class="card${p.pinned?' pinned':''}" data-id="${p.id}"><span class="cat cat-${p.category}">${p.category}</span><div class="body"><div class="val" id="v-${p.id}">${esc(p.value)}</div><div class="meta"><span><span class="bar-w"><span class="bar" style="width:${bw}px;background:${cc(p.confidence)}"></span></span><span class="cf">${Math.round(p.confidence*100)}%</span> confidence</span><span>✓ ${p.times_accepted} &nbsp;✗ ${p.times_rejected} &nbsp;✎ ${p.times_edited}</span><span>${sc}</span><span>${p.last_reinforced?rel(p.last_reinforced):'never used'}</span></div></div><div class="acts"><span class="pin ${p.pinned?'on':''}" onclick="pin('${p.id}',${p.pinned})">📌</span><button class="btn ok" onclick="edit('${p.id}')">Edit</button><button class="btn danger" onclick="del('${p.id}')">Delete</button></div></div>`;}).join('');}
async function pin(id,cur){await fetch('/api/preferences/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({pinned:!cur})});prefs();}
function edit(id){const p=all.find(x=>x.id===id);if(!p)return;document.getElementById('v-'+id).innerHTML=`<textarea class="edit-input" id="ei-${id}" rows="2">${esc(p.value)}</textarea><div style="display:flex;gap:6px;margin-top:6px"><button class="btn ok" onclick="save('${id}')">Save</button><button class="btn" onclick="prefs()">Cancel</button></div>`;}
async function save(id){const v=document.getElementById('ei-'+id).value.trim();if(!v)return;await fetch('/api/preferences/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:v})});prefs();}
async function del(id){if(!confirm('Delete this preference?'))return;await fetch('/api/preferences/'+id,{method:'DELETE'});prefs();stats();}
async function retrs(){const l=await fetch('/api/retrievals?user_id='+UID+'&limit=10').then(r=>r.json());const c=document.getElementById('retrs');if(!l.length){c.innerHTML='<div class="empty">No retrievals yet. Connect Claude and start a conversation.</div>';return;}c.innerHTML=l.map(r=>`<div class="ri"><div class="rh"><span class="rt">${r.task_type||'general'}</span><span class="rtime">${rel(r.created_at)}</span></div><div class="rp">${r.preferences_returned.length} preference${r.preferences_returned.length!==1?'s':''} returned${r.context_hint?' &middot; "'+esc(r.context_hint)+'"':''}</div><div class="rid">${r.id}</div></div>`).join('');}
document.getElementById('filters').addEventListener('click',e=>{if(!e.target.dataset.cat)return;filt=e.target.dataset.cat;document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.cat===filt));render();});
stats();prefs();retrs();setInterval(()=>{stats();prefs();retrs();},15000);
</script>
</body>
</html>"""

class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress request logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        uid = qs.get("user_id", ["local"])[0]

        if path == "/api/preferences":
            rows = db_query("SELECT * FROM preferences WHERE user_id=? ORDER BY confidence DESC, created_at DESC", (uid,))
            self.send_json([parse_pref(r) for r in rows])

        elif path == "/api/retrievals":
            limit = int(qs.get("limit", ["20"])[0])
            rows = db_query("SELECT * FROM retrieval_log WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (uid, limit))
            self.send_json([{**r, "preferences_returned": json.loads(r["preferences_returned"] or "[]"), "score_breakdown": json.loads(r["score_breakdown"] or "[]")} for r in rows])

        elif path == "/api/stats":
            prefs = len(db_query("SELECT id FROM preferences WHERE user_id=?", (uid,)))
            events = db_one("SELECT COUNT(*) as n FROM feedback_events WHERE user_id=?", (uid,))
            retrs  = db_one("SELECT COUNT(*) as n FROM retrieval_log WHERE user_id=?", (uid,))
            accepts= db_one("SELECT COUNT(*) as n FROM feedback_events WHERE user_id=? AND signal IN ('accepted','reused')", (uid,))
            total  = events["n"] if events else 0
            acc    = events["n"] if events else 0
            self.send_json({
                "preferences":    prefs,
                "feedback_events": total,
                "retrievals":     retrs["n"] if retrs else 0,
                "acceptance_rate": round((accepts["n"] / total) * 100) if total > 0 else None,
            })

        else:
            body = CONSOLE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/preferences/"):
            self.send_json({"error": "not found"}, 404)
            return
        pref_id = path.split("/")[-1]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        row = db_one("SELECT * FROM preferences WHERE id=?", (pref_id,))
        if not row:
            self.send_json({"error": "not found"}, 404)
            return

        if "value" in body:
            db_exec("UPDATE preferences SET value=? WHERE id=?", (body["value"], pref_id))
        if "pinned" in body:
            db_exec("UPDATE preferences SET pinned=? WHERE id=?", (1 if body["pinned"] else 0, pref_id))
        if "confidence" in body:
            db_exec("UPDATE preferences SET confidence=? WHERE id=?", (float(body["confidence"]), pref_id))
        if "task_scope" in body:
            sc = json.dumps(body["task_scope"]) if body["task_scope"] else None
            db_exec("UPDATE preferences SET task_scope=? WHERE id=?", (sc, pref_id))

        updated = db_one("SELECT * FROM preferences WHERE id=?", (pref_id,))
        self.send_json(parse_pref(updated))

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/preferences/"):
            self.send_json({"error": "not found"}, 404)
            return
        pref_id = path.split("/")[-1]
        db_exec("DELETE FROM preferences WHERE id=?", (pref_id,))
        self.send_json({"success": True})

def run_console():
    init_db()
    server = HTTPServer(("localhost", CONSOLE_PORT), ConsoleHandler)
    print(f"Coda console running at http://localhost:{CONSOLE_PORT}", file=sys.stderr)
    server.serve_forever()

# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--console" in sys.argv:
        run_console()
    else:
        run_mcp_server()
