# Coda — Preference-First Personalization for Claude

Coda is a personalization layer that persists, scores, and applies user preferences across every Claude session. It runs as a Cowork skill — no server, no infrastructure, 0 dependencies beyond Python 3 stdlib and SQLite.

---

## Why Coda, if Claude already adapts?

Claude adapts within a conversation. It forgets everything when the session ends.

The deeper distinction: Claude's adaptation is statistical, opaque, and uneditable. Coda's is structured, auditable, and fully under your control.

| | Claude (built-in) | Coda |
|---|---|---|
| Persists across sessions | No | Yes |
| Preferences are inspectable | No | Yes — every preference has an ID, value, score, and history |
| Preferences are editable | No | Yes — edit, pin, delete, or scope individual preferences |
| Adapts from feedback | Within context only | Across sessions, via scored feedback signals |
| Auditable decisions | No | Yes — `explain <retrieval_id>` shows exactly what scored and why |

Claude's learning happens inside a black box — in the weights, with 0 access. Coda's learning is a SQLite table you can read, correct, and control at any time.

---

## How it works

Preferences are stored in 6 structured categories: `tone`, `format`, `goals`, `constraints`, `habits`, `role`.

Each preference is scored per task using a weighted formula:

```
score = (task_match × 0.4) + (acceptance_rate × 0.3) + (recency × 0.2) + (confidence × 0.1)
```

Feedback signals update confidence automatically:

| Signal | Confidence delta |
|--------|-----------------|
| accepted | +0.05 |
| reused | +0.10 |
| edited | -0.03 |
| rejected | -0.10 |

Recency decays exponentially over a 14-day half-life. Pinned preferences always rank first, regardless of score.

---

## Files

```
coda-skill/
├── README.md               — this file
├── SKILL.md                — Claude's operating instructions
└── scripts/
    └── coda_engine.py      — preference engine CLI (zero dependencies)
```

Database: `~/.coda/coda.db` (override with `CODA_DB_PATH` env var)

---

## CLI reference

```bash
# Get top preferences for a task
python3 scripts/coda_engine.py get <writing|coding|planning|support|general>

# Add or update a preference
python3 scripts/coda_engine.py upsert --category tone --value "direct and formal"

# Log a feedback signal
python3 scripts/coda_engine.py feedback --signal accepted --retrieval-id <id> --pref-ids <id1,id2>

# List all stored preferences
python3 scripts/coda_engine.py list

# Delete a preference
python3 scripts/coda_engine.py delete <preference_id>

# Show stats
python3 scripts/coda_engine.py stats

# Explain a retrieval decision
python3 scripts/coda_engine.py explain <retrieval_id>
```

---

## Inspection console

The console provides a live UI for browsing preferences, confidence scores, and retrieval history.

```bash
CODA_DB_PATH=~/.coda/coda.db python3 server.py --console
```

Open `http://localhost:3456` in your browser.

---

## Benchmark results (iteration 1)

Tested across 3 eval tasks (product email, Series A metrics, technical summary):

| | With Coda | Without Coda |
|---|---|---|
| Pass rate | **100%** | 75% |
| Delta | **+25%** | — |

Key failures in baseline: filler language in formal writing, no preference persistence across prompts.
