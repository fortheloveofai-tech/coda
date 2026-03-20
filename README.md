# Coda

Preference-first personalization for Claude. Coda learns how you work and applies your preferences — tone, format, goals, constraints, habits, role — to every response. Automatically. Persistently. With full visibility into what it applied and why.

---

## Why Coda

Claude adapts within a conversation. It forgets everything when the session ends. Its adaptation is statistical, opaque, and uneditable.

Coda is different:

1. **Persists across sessions** — preferences survive conversation resets
2. **Scored, not stored** — every preference is ranked by task match (40%), acceptance rate (30%), recency (20%), and confidence (10%)
3. **Auditable** — inspect, edit, pin, or delete any preference via CLI or browser console
4. **Self-improving** — feedback signals adjust confidence automatically: accepted (+0.05), reused (+0.10), edited (-0.03), rejected (-0.10)

Claude's learning is a black box. Coda's is a SQLite table you control.

---

## What's in this repo

```
coda/
├── server.py          — MCP server (zero dependencies, Python stdlib only)
├── manifest.json      — MCP manifest for Claude Desktop
├── src/               — TypeScript source (tools, scoring, DB, console server)
├── coda-skill/        — Cowork skill (no server required)
│   ├── SKILL.md
│   ├── README.md
│   └── scripts/
│       └── coda_engine.py
└── package.json
```

Coda ships in 2 forms:

- **MCP server** (`server.py`) — for Claude Desktop and any MCP-compatible client
- **Cowork skill** (`coda-skill/`) — zero infrastructure, runs as a Claude skill

---

## Quick start — MCP server

Requires Python 3.10+. No pip installs.

```bash
# Run as MCP server (stdio)
python3 server.py

# Run inspection console at http://localhost:3456
python3 server.py --console
```

Add to Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "coda": {
      "command": "python3",
      "args": ["/path/to/coda/server.py"]
    }
  }
}
```

---

## Quick start — Cowork skill

1. Install `coda-skill/` as a Cowork skill
2. Preferences are stored at `~/.coda/coda.db` (auto-created on first use)
3. Coda runs silently on every qualifying task — no configuration required

---

## MCP tools

| Tool | Description |
|---|---|
| `coda_get_preferences` | Retrieve ranked preferences for the current task type |
| `coda_upsert_preference` | Create or update a preference |
| `coda_log_feedback` | Log accept / edit / reject signal |
| `coda_explain_retrieval` | Explain why specific preferences were returned |
| `coda_import_from_mem0` | Import Mem0 memories into structured preferences |

---

## Scoring formula

```
score = (task_match × 0.4) + (acceptance_rate × 0.3) + (recency × 0.2) + (confidence × 0.1)
```

- **task_match** — 1.0 if preference is scoped to current task type, 0.4 if general
- **acceptance_rate** — fraction of times the preference led to accepted outputs
- **recency** — exponential decay over a 14-day half-life
- **confidence** — starts at 0.7, updated on every feedback signal

Pinned preferences always rank first, regardless of score.

---

## Inspection console

```bash
CODA_DB_PATH=~/.coda/coda.db python3 server.py --console
```

Open `http://localhost:3456` — browse preferences, confidence scores, retrieval history. Edit, pin, or delete directly from the UI.

---

## Benchmark

Tested across 3 eval tasks (product email, Series A metrics, technical summary):

| | With Coda | Without Coda |
|---|---|---|
| Pass rate | **100%** | 75% |
| Delta | **+25%** | — |

---

## License

MIT
