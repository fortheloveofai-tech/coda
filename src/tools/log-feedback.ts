import { z } from 'zod';
import { nanoid } from 'nanoid';
import { db, parsePreferenceRow } from '../db.js';
import { applySignalDelta } from '../scoring.js';
import { Preference, Signal } from '../types.js';

export const LogFeedbackInput = z.object({
  user_id:                z.string().default('local'),
  retrieval_id:           z.string().optional(),
  signal:                 z.enum(['accepted', 'edited', 'rejected', 'reused']),
  task_type:              z.string().optional(),
  preference_ids_in_play: z.array(z.string()).default([]),
  edit_delta:             z.string().optional(),
});

export type LogFeedbackInput = z.infer<typeof LogFeedbackInput>;

// Which counter to increment per signal
const COUNTER_MAP: Record<Signal, string> = {
  accepted: 'times_accepted',
  reused:   'times_accepted',  // reuse is a strong accept
  edited:   'times_edited',
  rejected: 'times_rejected',
};

export function logFeedback(raw: LogFeedbackInput) {
  const { user_id, retrieval_id, signal, task_type, preference_ids_in_play, edit_delta }
    = LogFeedbackInput.parse(raw);

  // Record the event
  const event_id = `evt_${nanoid(10)}`;
  db.prepare(`
    INSERT INTO feedback_events (id, user_id, retrieval_id, signal, task_type, preference_ids, edit_delta)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `).run(
    event_id,
    user_id,
    retrieval_id ?? null,
    signal,
    task_type ?? null,
    JSON.stringify(preference_ids_in_play),
    edit_delta ?? null,
  );

  // Update confidence + counters for each affected preference (skip pinned ones)
  const updated = [];
  for (const pref_id of preference_ids_in_play) {
    const row = db.prepare('SELECT * FROM preferences WHERE id = ? AND user_id = ?')
      .get(pref_id, user_id) as Record<string, unknown> | undefined;

    if (!row) continue;

    const pref = parsePreferenceRow(row) as unknown as Preference;
    if (pref.pinned) continue; // pinned prefs are frozen

    const newConfidence = applySignalDelta(pref, signal as Signal);
    const counter = COUNTER_MAP[signal as Signal];

    db.prepare(`
      UPDATE preferences
      SET confidence = ?,
          ${counter} = ${counter} + 1,
          last_reinforced = datetime('now')
      WHERE id = ?
    `).run(newConfidence, pref_id);

    updated.push({
      id:               pref_id,
      confidence_delta: Math.round((newConfidence - pref.confidence) * 1000) / 1000,
      new_confidence:   Math.round(newConfidence * 1000) / 1000,
    });
  }

  return {
    success: true,
    event_id,
    preferences_updated: updated,
  };
}
