import Database from 'better-sqlite3';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.CODA_DB_PATH || path.join(__dirname, '..', 'coda.db');

export const db = new Database(DB_PATH);

// Enable WAL mode for better concurrent read performance
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

export function initDb(): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS preferences (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      category TEXT NOT NULL CHECK(category IN ('tone','format','goals','constraints','habits','role')),
      value TEXT NOT NULL,
      confidence REAL DEFAULT 0.7 CHECK(confidence >= 0 AND confidence <= 1),
      task_scope TEXT,          -- JSON array e.g. '["writing","coding"]', NULL = all tasks
      pinned INTEGER DEFAULT 0,
      source TEXT DEFAULT 'inferred' CHECK(source IN ('explicit','inferred','imported')),
      times_accepted INTEGER DEFAULT 0,
      times_rejected INTEGER DEFAULT 0,
      times_edited INTEGER DEFAULT 0,
      last_reinforced TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_preferences_user ON preferences(user_id);
    CREATE INDEX IF NOT EXISTS idx_preferences_user_category ON preferences(user_id, category);

    CREATE TABLE IF NOT EXISTS feedback_events (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      retrieval_id TEXT,
      signal TEXT NOT NULL CHECK(signal IN ('accepted','edited','rejected','reused')),
      task_type TEXT,
      preference_ids TEXT,      -- JSON array of pref IDs in play
      edit_delta TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_events(user_id);
    CREATE INDEX IF NOT EXISTS idx_feedback_retrieval ON feedback_events(retrieval_id);

    CREATE TABLE IF NOT EXISTS retrieval_log (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      task_type TEXT,
      context_hint TEXT,
      preferences_returned TEXT, -- JSON array of pref IDs
      score_breakdown TEXT,      -- JSON array of ScoreBreakdown
      created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_retrieval_user ON retrieval_log(user_id);
  `);
}

// Helper to parse a raw DB preference row into a proper Preference object
export function parsePreferenceRow(row: Record<string, unknown>) {
  return {
    ...row,
    task_scope: row.task_scope ? JSON.parse(row.task_scope as string) : null,
    pinned: Boolean(row.pinned),
  };
}
