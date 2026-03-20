import { z } from 'zod';
import { nanoid } from 'nanoid';
import { db, parsePreferenceRow } from '../db.js';
import { scorePreference, buildScoreBreakdown } from '../scoring.js';
import { Preference, TaskType } from '../types.js';

export const GetPreferencesInput = z.object({
  user_id:      z.string().default('local'),
  task_type:    z.enum(['writing', 'coding', 'planning', 'support', 'general']),
  context_hint: z.string().optional(),
  limit:        z.number().int().min(1).max(20).default(5),
});

export type GetPreferencesInput = z.infer<typeof GetPreferencesInput>;

export function getPreferences(raw: GetPreferencesInput) {
  const { user_id, task_type, context_hint, limit } = GetPreferencesInput.parse(raw);

  // Load all preferences for this user
  const rows = db.prepare('SELECT * FROM preferences WHERE user_id = ?').all(user_id) as Record<string, unknown>[];
  const prefs = rows.map(parsePreferenceRow) as unknown as Preference[];

  // Score, filter zeros, sort (pinned first, then by score)
  const scored = prefs
    .map(pref => ({ pref, score: scorePreference(pref, task_type as TaskType) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => {
      if (a.pref.pinned && !b.pref.pinned) return -1;
      if (!a.pref.pinned && b.pref.pinned) return 1;
      return b.score - a.score;
    });

  const top = scored.slice(0, limit);
  const scoreBreakdowns = top.map(({ pref }) => buildScoreBreakdown(pref, task_type as TaskType));

  // Log this retrieval so we can explain it later
  const retrieval_id = `ret_${nanoid(10)}`;
  db.prepare(`
    INSERT INTO retrieval_log (id, user_id, task_type, context_hint, preferences_returned, score_breakdown)
    VALUES (?, ?, ?, ?, ?, ?)
  `).run(
    retrieval_id,
    user_id,
    task_type,
    context_hint ?? null,
    JSON.stringify(top.map(({ pref }) => pref.id)),
    JSON.stringify(scoreBreakdowns),
  );

  // Build human-readable summary
  const counts: Record<string, number> = {};
  top.forEach(({ pref }) => { counts[pref.category] = (counts[pref.category] ?? 0) + 1; });
  const summary = top.length > 0
    ? `Returned ${top.length} preferences: ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ')}. Scored by task match + recency + acceptance rate + confidence.`
    : 'No preferences found for this user and task type yet. Outputs will improve as preferences are learned.';

  return {
    preferences: top.map(({ pref }) => ({
      id:              pref.id,
      category:        pref.category,
      value:           pref.value,
      confidence:      pref.confidence,
      task_scope:      pref.task_scope,
      times_accepted:  pref.times_accepted,
      last_reinforced: pref.last_reinforced,
      pinned:          pref.pinned,
    })),
    retrieval_id,
    retrieval_summary: summary,
  };
}
