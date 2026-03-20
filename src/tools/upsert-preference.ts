import { z } from 'zod';
import { nanoid } from 'nanoid';
import { db, parsePreferenceRow } from '../db.js';

export const UpsertPreferenceInput = z.object({
  user_id:    z.string().default('local'),
  category:   z.enum(['tone', 'format', 'goals', 'constraints', 'habits', 'role']),
  value:      z.string().min(1).max(500),
  task_scope: z.array(z.enum(['writing', 'coding', 'planning', 'support', 'general'])).optional(),
  confidence: z.number().min(0).max(1).default(0.7),
  pinned:     z.boolean().default(false),
  source:     z.enum(['explicit', 'inferred', 'imported']).default('inferred'),
});

export type UpsertPreferenceInput = z.infer<typeof UpsertPreferenceInput>;

export function upsertPreference(raw: UpsertPreferenceInput) {
  const { user_id, category, value, task_scope, confidence, pinned, source }
    = UpsertPreferenceInput.parse(raw);

  const taskScopeJson = task_scope && task_scope.length > 0
    ? JSON.stringify(task_scope)
    : null;

  // Look for an existing preference with the same user + category + value
  const existing = db.prepare(`
    SELECT * FROM preferences WHERE user_id = ? AND category = ? AND value = ?
  `).get(user_id, category, value) as Record<string, unknown> | undefined;

  if (existing) {
    // Update: keep the higher confidence, and honour explicit pin requests
    const prevConf = existing.confidence as number;
    db.prepare(`
      UPDATE preferences
      SET confidence = ?,
          task_scope = COALESCE(?, task_scope),
          pinned = CASE WHEN ? = 1 THEN 1 ELSE pinned END,
          source = ?,
          last_reinforced = datetime('now')
      WHERE id = ?
    `).run(
      Math.max(prevConf, confidence),
      taskScopeJson,
      pinned ? 1 : 0,
      source,
      existing.id,
    );

    const updated = db.prepare('SELECT * FROM preferences WHERE id = ?').get(existing.id) as Record<string, unknown>;
    return { preference: parsePreferenceRow(updated), action: 'updated' };
  }

  // Create new
  const id = `pref_${nanoid(10)}`;
  db.prepare(`
    INSERT INTO preferences (id, user_id, category, value, confidence, task_scope, pinned, source)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `).run(id, user_id, category, value, confidence, taskScopeJson, pinned ? 1 : 0, source);

  const created = db.prepare('SELECT * FROM preferences WHERE id = ?').get(id) as Record<string, unknown>;
  return { preference: parsePreferenceRow(created), action: 'created' };
}
