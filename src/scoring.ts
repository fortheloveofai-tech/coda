import { Preference, TaskType, ScoreBreakdown, Signal } from './types.js';

// How much each behavioral signal shifts confidence
export const SIGNAL_WEIGHTS: Record<Signal, number> = {
  accepted: +0.05,
  reused:   +0.10,  // strongest positive signal — user came back for it
  edited:   -0.03,  // partial — close but not quite right
  rejected: -0.10,  // clear negative
};

/**
 * Score a preference for a given task type.
 * Returns 0 if the preference is explicitly scoped away from this task.
 *
 * score = (task_match × 0.4) + (acceptance_rate × 0.3) + (recency × 0.2) + (confidence × 0.1)
 */
export function scorePreference(pref: Preference, taskType: TaskType): number {
  const taskMatch = getTaskMatchWeight(pref.task_scope, taskType);

  // Hard exclude — this preference doesn't apply to this task type
  if (taskMatch === 0) return 0;

  const totalSignals = pref.times_accepted + pref.times_rejected + pref.times_edited;
  // Neutral prior of 0.5 when no signals yet
  const acceptanceRate = totalSignals > 0
    ? pref.times_accepted / totalSignals
    : 0.5;

  const recencyScore = getRecencyScore(pref.last_reinforced);

  return (taskMatch * 0.4)
    + (acceptanceRate * 0.3)
    + (recencyScore * 0.2)
    + (pref.confidence * 0.1);
}

/**
 * Returns a full breakdown of why a preference scored the way it did.
 * Used by the inspection console and the explain_retrieval tool.
 */
export function buildScoreBreakdown(pref: Preference, taskType: TaskType): ScoreBreakdown {
  const taskMatch = getTaskMatchWeight(pref.task_scope, taskType);
  const totalSignals = pref.times_accepted + pref.times_rejected + pref.times_edited;
  const acceptanceRate = totalSignals > 0 ? pref.times_accepted / totalSignals : 0.5;
  const recencyScore = getRecencyScore(pref.last_reinforced);

  return {
    preference_id: pref.id,
    value: pref.value,
    task_match: round(taskMatch),
    acceptance_rate: round(acceptanceRate),
    recency_score: round(recencyScore),
    confidence: round(pref.confidence),
    final_score: round(scorePreference(pref, taskType)),
  };
}

/**
 * Apply a behavioral signal to a preference's confidence score.
 * Clamps result to [0.1, 1.0] so preferences never fully disappear.
 */
export function applySignalDelta(pref: Preference, signal: Signal): number {
  const delta = SIGNAL_WEIGHTS[signal];
  return Math.max(0.1, Math.min(1.0, pref.confidence + delta));
}

// ── Internals ──────────────────────────────────────────────────────────────

function getTaskMatchWeight(taskScope: TaskType[] | null, taskType: TaskType): number {
  if (!taskScope || taskScope.length === 0) return 0.4; // general preference
  if (taskScope.includes(taskType)) return 1.0;          // perfect match
  return 0.0;                                            // explicitly excluded
}

function getRecencyScore(lastReinforced: string | null): number {
  if (!lastReinforced) return 0.3; // never used — give it a fair starting chance

  const daysSince = (Date.now() - new Date(lastReinforced).getTime()) / 86_400_000;
  // Exponential decay: 1.0 at day 0, ~0.5 at day 10, ~0.1 at day 32
  return Math.max(0.1, Math.exp(-daysSince / 14));
}

function round(n: number): number {
  return Math.round(n * 1000) / 1000;
}
