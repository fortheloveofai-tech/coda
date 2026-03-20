---
name: coda
description: >
  Use this skill when a user message contains ANY of these: a statement about who
  they are ("I'm a founder/engineer/designer"), a rule about how Claude should always
  or never respond, a tone or format constraint ("formal tone", "keep it tight", "no
  exclamation marks"), a complaint or correction about a previous response's style or
  length, or a question about what Claude remembers or knows about them. Also use when
  a task request is bundled with a style constraint — the constraint is the trigger.
  Do NOT use for pure task requests with no self-description, no format rules, and no
  feedback on prior responses.
---

# Coda — Preference-First Personalization

Coda learns how the user likes to work and applies those preferences silently to every response. Preferences are stored locally in a SQLite database and scored using a weighted formula: task match, acceptance rate, recency, and confidence.

## Quick-start: What to do on every task

1. **Run `get`** at the start of any substantive task (skip for tiny one-liners)
2. **Apply the preferences** naturally — don't announce that you're doing it
3. **After delivering** your response, check for feedback signals and log them
4. **When the user states a preference** explicitly, upsert it immediately

---

## Script location

All preference operations go through:
```
<skill-dir>/scripts/coda_engine.py
```

The DB lives at `~/.coda/coda.db` by default. Override with `CODA_DB_PATH` env var.

---

## Commands

### Get preferences for a task

```bash
python <skill-dir>/scripts/coda_engine.py get <task_type> [--context "hint"] [--limit 5]
```

`task_type` is one of: `writing`, `coding`, `planning`, `support`, `general`

Returns: a list of ranked preferences + a `retrieval_id` for feedback logging.

**Example output:**
```json
{
  "preferences": [
    {
      "id": "pref_abc123",
      "category": "tone",
      "value": "direct and concise — skip preamble",
      "confidence": 0.92,
      "task_scope": ["writing", "coding"],
      "times_accepted": 14,
      "pinned": false
    }
  ],
  "retrieval_id": "ret_xyz456",
  "retrieval_summary": "Returned 3 preferences: 1 tone, 1 format, 1 constraints."
}
```

Save the `retrieval_id` — you'll need it to log feedback later.

---

### Upsert a preference

Call this when the user explicitly states how they want things done.

```bash
python <skill-dir>/scripts/coda_engine.py upsert \
  --category <category> \
  --value "<description>" \
  [--task-scope "writing,coding"] \
  [--confidence 0.9] \
  [--pinned] \
  [--source explicit|inferred|imported]
```

**Categories:**
- `tone` — communication style (direct, formal, casual, concise, verbose, professional)
- `format` — output structure (bullet points, markdown, tables, numbered lists, headings)
- `goals` — what the user is working toward or building
- `constraints` — things to avoid, limits, hard rules ("never exceed 200 words")
- `habits` — how they typically work ("always starts with an outline")
- `role` — their job/expertise ("senior iOS engineer at a startup")

**When to infer vs. require explicit:**
- Use `--source explicit` when the user directly states a preference
- Use `--source inferred` when you observe a strong pattern from their behavior
- Set confidence 0.9+ for explicit, 0.6–0.75 for inferred
- Only infer if the signal is clear (e.g., they've corrected the same thing 3+ times)

---

### Log feedback after a response

```bash
python <skill-dir>/scripts/coda_engine.py feedback \
  --signal <accepted|edited|rejected|reused> \
  [--retrieval-id <ret_xyz456>] \
  [--pref-ids "pref_abc123,pref_def456"] \
  [--task-type writing] \
  [--edit-delta "removed the intro paragraph"]
```

**Signal meanings:**
- `accepted` — user took the output as-is, no corrections
- `reused` — user explicitly reused or copied the output later
- `edited` — user modified the output (minor negative signal)
- `rejected` — user asked to redo it or said "not like that"

You don't need to log feedback after every message. Log it when:
- The user accepts a substantial piece of work without editing
- The user edits a specific section (include what changed as `edit-delta`)
- The user rejects and asks for a redo
- The user explicitly praises or criticizes the output

---

### List all preferences

```bash
python <skill-dir>/scripts/coda_engine.py list [--category tone]
```

Use this when the user asks "what do you know about me?" or "what preferences have you stored?"

---

### Delete a preference

```bash
python <skill-dir>/scripts/coda_engine.py delete <preference_id>
```

Use when the user says "forget that" or "that's not right anymore."

---

### Stats

```bash
python <skill-dir>/scripts/coda_engine.py stats
```

Shows counts, acceptance rate, and DB path. Useful for "how's my personalization going?"

---

### Explain a retrieval

```bash
python <skill-dir>/scripts/coda_engine.py explain <retrieval_id>
```

Shows why each preference was (or wasn't) returned. Use when the user asks why Claude responded a certain way.

---

## Applying preferences — how to do it well

When you get preferences back, apply them silently. Don't say "Based on your preference for X, I will Y." Just do Y.

**Examples:**

- `tone: "direct and concise — skip preamble"` → Start with the answer, not "Great question! Let me..."
- `format: "always use bullet points for lists"` → Use bullets, not prose enumeration
- `constraints: "no more than 3 paragraphs"` → Enforce the limit
- `role: "senior engineer"` → Skip beginner explanations, use precise technical terms
- `goals: "building a B2B SaaS product"` → Frame suggestions in that context

If preferences conflict, prefer the more specific one (task-scoped wins over general).

---

## Noticing new preferences during a conversation

Stay alert for signals the user hasn't explicitly stated but consistently shows:

| Signal | Action |
|--------|--------|
| User restates the same correction | Infer a constraint, confidence 0.65 |
| User says "always do X" / "never do Y" | Upsert explicit, confidence 0.9 |
| User copies your output without changes | Log `accepted` |
| User pastes your output and edits it | Log `edited`, note what changed |
| User says "perfect" or "exactly right" | Log `accepted` or `reused` |
| User says "ugh no" or rewrites from scratch | Log `rejected` |

---

## Scoring formula (for reference)

Each preference is scored against the current task:

```
score = (task_match × 0.4) + (acceptance_rate × 0.3) + (recency × 0.2) + (confidence × 0.1)
```

- **task_match**: 1.0 if task_scope matches, 0.4 if general, 0.0 if out of scope
- **acceptance_rate**: fraction of times the preference led to accepted outputs
- **recency**: exponential decay over 14-day half-life
- **confidence**: starts at 0.7, bumped by +0.05 on accept, -0.10 on reject

Pinned preferences always appear first, regardless of score.

---

## Common flows

### First time — no preferences yet
```
get general → empty result → proceed normally → watch for explicit statements → upsert any you hear
```

### Ongoing task with preferences
```
get <task_type> → apply top preferences → deliver → (user happy? log accepted) → (user edits? log edited + note delta)
```

### User states a preference mid-conversation
```
"Can you stop using so many headers?"
→ upsert --category format --value "avoid headers, use flowing prose instead" --source explicit
→ apply immediately to your next response
```

### User asks what you know about them
```
list → present the preferences in a readable, conversational way
→ offer to delete or update any that are wrong
```
