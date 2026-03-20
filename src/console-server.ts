import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';
import { db, initDb, parsePreferenceRow } from './db.js';
import { Preference } from './types.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = parseInt(process.env.CODA_CONSOLE_PORT ?? '3456', 10);

initDb();

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'console')));

// ── REST API ────────────────────────────────────────────────────────────────

// GET /api/preferences?user_id=local
app.get('/api/preferences', (req, res) => {
  const user_id = (req.query.user_id as string) || 'local';
  const rows = db.prepare('SELECT * FROM preferences WHERE user_id = ? ORDER BY confidence DESC, created_at DESC')
    .all(user_id) as Record<string, unknown>[];
  res.json(rows.map(parsePreferenceRow));
});

// PATCH /api/preferences/:id  — update value, pinned, or task_scope
app.patch('/api/preferences/:id', (req, res) => {
  const { id } = req.params;
  const { value, pinned, task_scope, confidence } = req.body as {
    value?: string;
    pinned?: boolean;
    task_scope?: string[] | null;
    confidence?: number;
  };

  const pref = db.prepare('SELECT * FROM preferences WHERE id = ?').get(id);
  if (!pref) { res.status(404).json({ error: 'Not found' }); return; }

  db.prepare(`
    UPDATE preferences SET
      value      = COALESCE(?, value),
      pinned     = COALESCE(?, pinned),
      task_scope = CASE WHEN ? IS NOT NULL THEN ? ELSE task_scope END,
      confidence = COALESCE(?, confidence)
    WHERE id = ?
  `).run(
    value ?? null,
    pinned !== undefined ? (pinned ? 1 : 0) : null,
    task_scope !== undefined ? 1 : null,
    task_scope !== undefined ? (task_scope ? JSON.stringify(task_scope) : null) : null,
    confidence ?? null,
    id,
  );

  const updated = db.prepare('SELECT * FROM preferences WHERE id = ?').get(id) as Record<string, unknown>;
  res.json(parsePreferenceRow(updated));
});

// DELETE /api/preferences/:id
app.delete('/api/preferences/:id', (req, res) => {
  db.prepare('DELETE FROM preferences WHERE id = ?').run(req.params.id);
  res.json({ success: true });
});

// GET /api/retrievals?user_id=local&limit=20
app.get('/api/retrievals', (req, res) => {
  const user_id = (req.query.user_id as string) || 'local';
  const limit = parseInt((req.query.limit as string) || '20', 10);
  const rows = db.prepare(`
    SELECT * FROM retrieval_log WHERE user_id = ?
    ORDER BY created_at DESC LIMIT ?
  `).all(user_id, limit) as Record<string, unknown>[];
  res.json(rows.map(r => ({
    ...r,
    preferences_returned: JSON.parse(r.preferences_returned as string ?? '[]'),
    score_breakdown:      JSON.parse(r.score_breakdown as string ?? '[]'),
  })));
});

// GET /api/stats?user_id=local
app.get('/api/stats', (req, res) => {
  const user_id = (req.query.user_id as string) || 'local';
  const prefs = db.prepare('SELECT * FROM preferences WHERE user_id = ?').all(user_id).length;
  const events = db.prepare('SELECT COUNT(*) as n FROM feedback_events WHERE user_id = ?').get(user_id) as { n: number };
  const retrievals = db.prepare('SELECT COUNT(*) as n FROM retrieval_log WHERE user_id = ?').get(user_id) as { n: number };
  const accepts = db.prepare("SELECT COUNT(*) as n FROM feedback_events WHERE user_id = ? AND signal IN ('accepted','reused')").get(user_id) as { n: number };
  res.json({
    preferences: prefs,
    feedback_events: events.n,
    retrievals: retrievals.n,
    acceptance_rate: events.n > 0 ? Math.round((accepts.n / events.n) * 100) : null,
  });
});

// Serve the SPA for any other route
app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'console', 'index.html'));
});

app.listen(PORT, () => {
  process.stderr.write(`Coda console running at http://localhost:${PORT}\n`);
});
