export type TaskType = 'writing' | 'coding' | 'planning' | 'support' | 'general';
export type Category = 'tone' | 'format' | 'goals' | 'constraints' | 'habits' | 'role';
export type Signal = 'accepted' | 'edited' | 'rejected' | 'reused';
export type Source = 'explicit' | 'inferred' | 'imported';

export interface Preference {
  id: string;
  user_id: string;
  category: Category;
  value: string;
  confidence: number;
  task_scope: TaskType[] | null; // null = applies to all tasks
  pinned: boolean;
  source: Source;
  times_accepted: number;
  times_rejected: number;
  times_edited: number;
  last_reinforced: string | null;
  created_at: string;
}

export interface FeedbackEvent {
  id: string;
  user_id: string;
  retrieval_id: string | null;
  signal: Signal;
  task_type: TaskType | null;
  preference_ids: string[];
  edit_delta: string | null;
  created_at: string;
}

export interface RetrievalLog {
  id: string;
  user_id: string;
  task_type: TaskType;
  context_hint: string | null;
  preferences_returned: string[];
  score_breakdown: ScoreBreakdown[];
  created_at: string;
}

export interface ScoreBreakdown {
  preference_id: string;
  value: string;
  task_match: number;
  acceptance_rate: number;
  recency_score: number;
  confidence: number;
  final_score: number;
}
