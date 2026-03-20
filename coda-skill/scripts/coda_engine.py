#!/usr/bin/env python3
"""
Coda Preference Engine — standalone CLI for use inside a Cowork skill.

Commands:
  get       <task_type> [--context <hint>] [--limit N]
  upsert    --category <cat> --value <val> [--task-scope t1,t2] [--confidence N] [--pinned] [--source S]
  feedback  --signal <sig> [--retrieval-id <id>] [--pref-ids id1,id2] [--task-type T] [--edit-delta D]
  explain   <retrieval_id>
  list      [--category <cat>]
  delete    <preference_id>
  stats

All output is JSON for easy parsing by Claude.
"""

import sys
import json
import sqlite3
import os
import math
import uuid
import argparse
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("CODA_DB_PATH") or os.path.join(
    os.path.expanduser("~"), ".coda", "coda.db"
)

# ── Database ────────────────────────────────────────────────────────────────

def ensure_db_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def get_db():
    ensure_db_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS preferences (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'local',
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
            user_id TEXT NOT NULL DEFAULT 'local',
            retrieval_id TEXT,
            signal TEXT NOT NULL,
            task_type TEXT,
            preference_ids TEXT,
            edit_delta TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS retrieval_log (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'local',
            task_type TEXT,
            context_hint TEXT,
            preferences_returned TEXT,
            score_breakdown TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

def parse_pref(row):
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
COUNTER_MAP = {"accepted": "times_accepted", "reused": "times_accepted",
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
        "preference_id": pref["id"],
        "category": pref["category"],
        "value": pref["value"],
        "task_match": round(tm, 3),
        "acceptance_rate": round(ar, 3),
        "recency_score": round(rs, 3),
        "confidence": round(pref["confidence"], 3),
        "final_score": round(score_pref(pref, task_type), 3),
    }

def apply_signal(confidence, signal):
    delta = SIGNAL_WEIGHTS.get(signal, 0)
    return max(0.1, min(1.0, confidence + delta))

# ── Commands ────────────────────────────────────────────────────────────────

def cmd_get(conn, args):
    task_type = args.task_type or "general"
    limit = min(args.limit or 5, 20)

    rows = conn.execute("SELECT * FROM preferences WHERE user_id = ?", ("local",)).fetchall()
    prefs = [parse_pref(r) for r in rows]

    scored = [(p, score_pref(p, task_type)) for p in prefs]
    scored = [(p, s) for p, s in scored if s > 0]
    scored.sort(key=lambda x: (not x[0]["pinned"], -x[1]))

    top = scored[:limit]
    breakdowns = [score_breakdown(p, task_type) for p, _ in top]

    retrieval_id = make_id("ret")
    conn.execute(
        "INSERT INTO retrieval_log (id, user_id, task_type, context_hint, preferences_returned, score_breakdown) VALUES (?,?,?,?,?,?)",
        (retrieval_id, "local", task_type, args.context,
         json.dumps([p["id"] for p, _ in top]),
         json.dumps(breakdowns))
    )
    conn.commit()

    from collections import Counter
    cats = Counter(p["category"] for p, _ in top)
    summary = (f"Returned {len(top)} preferences: " +
               ", ".join(f"{v} {k}" for k, v in cats.items()) +
               ". Scored by task match + recency + acceptance rate + confidence."
               ) if top else "No preferences found yet. They will be learned over time."

    return {
        "preferences": [{
            "id": p["id"],
            "category": p["category"],
            "value": p["value"],
            "confidence": p["confidence"],
            "task_scope": p["task_scope"],
            "times_accepted": p["times_accepted"],
            "last_reinforced": p.get("last_reinforced"),
            "pinned": p["pinned"],
        } for p, _ in top],
        "retrieval_id": retrieval_id,
        "retrieval_summary": summary,
        "score_breakdown": breakdowns,
    }

def cmd_upsert(conn, args):
    category = args.category
    value = args.value.strip()
    task_scope = args.task_scope.split(",") if args.task_scope else None
    confidence = args.confidence or 0.9
    pinned = args.pinned
    source = args.source or "explicit"

    if not category or not value:
        return {"error": "category and value are required"}

    scope_json = json.dumps(task_scope) if task_scope else None

    existing = conn.execute(
        "SELECT * FROM preferences WHERE user_id=? AND category=? AND value=?",
        ("local", category, value)
    ).fetchone()

    if existing:
        existing = dict(existing)
        new_conf = max(existing["confidence"], confidence)
        conn.execute(
            "UPDATE preferences SET confidence=?, task_scope=COALESCE(?,task_scope), pinned=CASE WHEN ? THEN 1 ELSE pinned END, source=?, last_reinforced=datetime('now') WHERE id=?",
            (new_conf, scope_json, 1 if pinned else 0, source, existing["id"])
        )
        conn.commit()
        row = dict(conn.execute("SELECT * FROM preferences WHERE id=?", (existing["id"],)).fetchone())
        return {"preference": parse_pref(row), "action": "updated"}

    pref_id = make_id("pref")
    conn.execute(
        "INSERT INTO preferences (id, user_id, category, value, confidence, task_scope, pinned, source) VALUES (?,?,?,?,?,?,?,?)",
        (pref_id, "local", category, value, confidence, scope_json, 1 if pinned else 0, source)
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM preferences WHERE id=?", (pref_id,)).fetchone())
    return {"preference": parse_pref(row), "action": "created"}

def cmd_feedback(conn, args):
    signal = args.signal
    retrieval_id = args.retrieval_id
    task_type = args.task_type
    pref_ids = args.pref_ids.split(",") if args.pref_ids else []
    edit_delta = args.edit_delta

    event_id = make_id("evt")
    conn.execute(
        "INSERT INTO feedback_events (id, user_id, retrieval_id, signal, task_type, preference_ids, edit_delta) VALUES (?,?,?,?,?,?,?)",
        (event_id, "local", retrieval_id, signal, task_type, json.dumps(pref_ids), edit_delta)
    )

    updated = []
    for pref_id in pref_ids:
        row = conn.execute("SELECT * FROM preferences WHERE id = ? AND user_id = ?", (pref_id, "local")).fetchone()
        if not row:
            continue
        row = dict(row)
        if row.get("pinned"):
            continue
        new_conf = apply_signal(row["confidence"], signal)
        counter = COUNTER_MAP.get(signal, "times_accepted")
        conn.execute(
            f"UPDATE preferences SET confidence=?, {counter}={counter}+1, last_reinforced=datetime('now') WHERE id=?",
            (new_conf, pref_id)
        )
        updated.append({
            "id": pref_id,
            "confidence_delta": round(new_conf - row["confidence"], 3),
            "new_confidence": round(new_conf, 3),
        })

    conn.commit()
    return {"success": True, "event_id": event_id, "preferences_updated": updated}

def cmd_explain(conn, args):
    retrieval_id = args.retrieval_id
    log = conn.execute("SELECT * FROM retrieval_log WHERE id=?", (retrieval_id,)).fetchone()
    if not log:
        return {"error": f'Retrieval "{retrieval_id}" not found.'}
    log = dict(log)

    returned_ids = set(json.loads(log["preferences_returned"] or "[]"))
    breakdown = json.loads(log["score_breakdown"] or "[]")
    all_prefs = [parse_pref(dict(r)) for r in conn.execute("SELECT * FROM preferences WHERE user_id=?", ("local",)).fetchall()]

    excluded = []
    for p in all_prefs:
        if p["id"] in returned_ids:
            continue
        s = score_pref(p, log["task_type"] or "general")
        if s == 0 and p["task_scope"] and log["task_type"] not in (p["task_scope"] or []):
            reason = f"task_scope mismatch - scoped to [{', '.join(p['task_scope'])}] only"
        elif s == 0:
            reason = "score was 0 for this task type"
        else:
            reason = f"ranked below top {len(returned_ids)} (score: {s:.3f})"
        excluded.append({"preference_id": p["id"], "value": p["value"], "reason": reason})

    return {
        "retrieval_id": retrieval_id,
        "task_type": log["task_type"],
        "context_hint": log["context_hint"],
        "preferences_returned": len(returned_ids),
        "score_breakdown": breakdown,
        "excluded_preferences": excluded,
    }

def cmd_list(conn, args):
    if args.category:
        rows = conn.execute("SELECT * FROM preferences WHERE user_id=? AND category=? ORDER BY confidence DESC", ("local", args.category)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM preferences WHERE user_id=? ORDER BY category, confidence DESC", ("local",)).fetchall()
    return {"preferences": [parse_pref(dict(r)) for r in rows], "count": len(rows)}

def cmd_delete(conn, args):
    conn.execute("DELETE FROM preferences WHERE id=? AND user_id=?", (args.preference_id, "local"))
    conn.commit()
    return {"success": True, "deleted": args.preference_id}

def cmd_stats(conn, args):
    prefs = conn.execute("SELECT COUNT(*) as n FROM preferences WHERE user_id=?", ("local",)).fetchone()["n"]
    events = conn.execute("SELECT COUNT(*) as n FROM feedback_events WHERE user_id=?", ("local",)).fetchone()["n"]
    retrs = conn.execute("SELECT COUNT(*) as n FROM retrieval_log WHERE user_id=?", ("local",)).fetchone()["n"]
    accepts = conn.execute("SELECT COUNT(*) as n FROM feedback_events WHERE user_id=? AND signal IN ('accepted','reused')", ("local",)).fetchone()["n"]

    by_cat = {}
    for row in conn.execute("SELECT category, COUNT(*) as n FROM preferences WHERE user_id=? GROUP BY category", ("local",)).fetchall():
        by_cat[row["category"]] = row["n"]

    return {
        "preferences": prefs,
        "feedback_events": events,
        "retrievals": retrs,
        "acceptance_rate": round((accepts / events) * 100) if events > 0 else None,
        "by_category": by_cat,
        "db_path": DB_PATH,
    }

# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Coda Preference Engine")
    sub = parser.add_subparsers(dest="command")

    # get
    p_get = sub.add_parser("get", help="Get ranked preferences for a task type")
    p_get.add_argument("task_type", nargs="?", default="general")
    p_get.add_argument("--context", default=None)
    p_get.add_argument("--limit", type=int, default=5)

    # upsert
    p_up = sub.add_parser("upsert", help="Create or update a preference")
    p_up.add_argument("--category", required=True, choices=["tone", "format", "goals", "constraints", "habits", "role"])
    p_up.add_argument("--value", required=True)
    p_up.add_argument("--task-scope", default=None, help="Comma-separated task types")
    p_up.add_argument("--confidence", type=float, default=0.9)
    p_up.add_argument("--pinned", action="store_true")
    p_up.add_argument("--source", default="explicit", choices=["explicit", "inferred", "imported"])

    # feedback
    p_fb = sub.add_parser("feedback", help="Log a feedback signal")
    p_fb.add_argument("--signal", required=True, choices=["accepted", "edited", "rejected", "reused"])
    p_fb.add_argument("--retrieval-id", default=None)
    p_fb.add_argument("--pref-ids", default=None, help="Comma-separated preference IDs")
    p_fb.add_argument("--task-type", default=None)
    p_fb.add_argument("--edit-delta", default=None)

    # explain
    p_ex = sub.add_parser("explain", help="Explain a retrieval decision")
    p_ex.add_argument("retrieval_id")

    # list
    p_ls = sub.add_parser("list", help="List all preferences")
    p_ls.add_argument("--category", default=None)

    # delete
    p_del = sub.add_parser("delete", help="Delete a preference")
    p_del.add_argument("preference_id")

    # stats
    sub.add_parser("stats", help="Show stats")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = get_db()
    init_db(conn)

    commands = {
        "get": cmd_get,
        "upsert": cmd_upsert,
        "feedback": cmd_feedback,
        "explain": cmd_explain,
        "list": cmd_list,
        "delete": cmd_delete,
        "stats": cmd_stats,
    }

    result = commands[args.command](conn, args)
    print(json.dumps(result, indent=2))
    conn.close()

if __name__ == "__main__":
    main()
