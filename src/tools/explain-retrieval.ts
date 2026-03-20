import { z } from 'zod';
import { db, parsePreferenceRow } from '../db.js';
import { Preference, TaskType } from '../types.js';
import { scorePreference } from '../scoring.js';

export const ExplainRetrievalInput = z.object({
  retrieval_id: z.string(),
});

export type ExplainRetrievalInput = z.infer<typeof ExplainRetrievalInput>;

export function explainRetrieval(raw: ExplainRetrievalInput) {
  const { retrieval_id } = ExplainRetrievalInput.parse(raw);

  const log = db.prepare('SELECT * FROM retrieval_log WHERE id = ?').get(retrieval_id) as Record<string, unknown> | undefined;

  if (!log) {
    return { error: `Retrieval "${retrieval_id}" not found.` };
  }

  const scoreBreakdown = JSON.parse(log.score_breakdown as string ?? '[]');
  const preferencesReturned: string[] = JSON.parse(log.preferences_returned as string ?? '[]');
  const returnedSet = new Set(preferencesReturned);

  // Find what was considered but not returned
  const allRows = db.prepare('SELECT * FROM preferences WHERE user_id = ?').all(log.user_id) as Record<string, unknown>[];
  const allPrefs = allRows.map(parsePreferenceRow) as unknown as Preference[];

  const excluded = allPrefs
    .filter(p => !returnedSet.has(p.id))
    .map(p => {
      const score = scorePreference(p, log.task_type as TaskType);
      let reason: string;
      if (score === 0 && p.task_scope && !(p.task_scope as string[]).includes(log.task_type as string)) {
        reason = `task_scope mismatch — preference scoped to [${(p.task_scope as string[]).join(', ')}] only`;
      } else if (score === 0) {
        reason = 'score was 0 for this task type';
      } else {
        reason = `ranked below top ${preferencesReturned.length} (score: ${score.toFixed(3)})`;
      }
      return { preference_id: p.id, value: p.value, reason };
    });

  return {
    retrieval_id,
    task_type:              log.task_type,
    context_hint:           log.context_hint,
    created_at:             log.created_at,
    preferences_considered: allPrefs.length,
    preferences_returned:   preferencesReturned.length,
    score_breakdown:        scoreBreakdown,
    excluded_preferences:   excluded,
  };
}
