import { z } from 'zod';
import { nanoid } from 'nanoid';
import { db } from '../db.js';

export const ImportFromMem0Input = z.object({
  user_id:      z.string().default('local'),
  mem0_user_id: z.string(),
  mem0_api_key: z.string(),
  dry_run:      z.boolean().default(false),
});

export type ImportFromMem0Input = z.infer<typeof ImportFromMem0Input>;

// Simple heuristic rules to map raw memory text → a preference category + scope
const CLASSIFIERS: Array<{
  pattern: RegExp;
  category: string;
  task_scope?: string[];
}> = [
  { pattern: /\b(tone|direct|formal|casual|friendly|professional|concise|brief|verbose|jargon)\b/i,
    category: 'tone', task_scope: ['writing'] },
  { pattern: /\b(bullet|format|markdown|table|heading|structure|layout|numbered|bold)\b/i,
    category: 'format' },
  { pattern: /\b(goal|objective|aim|trying to|want to|building|working on)\b/i,
    category: 'goals' },
  { pattern: /\b(don'?t|avoid|never|constraint|limit|restrict|must not|no more than)\b/i,
    category: 'constraints' },
  { pattern: /\b(always|usually|typically|prefer|habit|tend to|every time)\b/i,
    category: 'habits' },
  { pattern: /\b(engineer|developer|designer|manager|analyst|role|title|senior|junior|lead|founder)\b/i,
    category: 'role' },
];

function classify(text: string): { category: string; task_scope?: string[] } | null {
  for (const { pattern, category, task_scope } of CLASSIFIERS) {
    if (pattern.test(text)) return { category, task_scope };
  }
  return null;
}

export async function importFromMem0(raw: ImportFromMem0Input) {
  const { user_id, mem0_user_id, mem0_api_key, dry_run } = ImportFromMem0Input.parse(raw);

  // Fetch from Mem0 REST API
  let memories: { memory: string }[] = [];
  try {
    const res = await fetch(`https://api.mem0.ai/v1/memories/?user_id=${encodeURIComponent(mem0_user_id)}`, {
      headers: {
        'Authorization': `Token ${mem0_api_key}`,
        'Content-Type': 'application/json',
      },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json() as unknown;
    memories = Array.isArray(data) ? data : (data as { results: { memory: string }[] }).results ?? [];
  } catch (err) {
    return { error: `Failed to fetch from Mem0: ${(err as Error).message}` };
  }

  let created = 0;
  let skipped = 0;
  const preview: Array<{ mem0_memory: string; mapped_to: object }> = [];

  const insert = db.prepare(`
    INSERT OR IGNORE INTO preferences (id, user_id, category, value, confidence, task_scope, source)
    VALUES (?, ?, ?, ?, ?, ?, 'imported')
  `);

  for (const mem of memories) {
    const text = mem.memory ?? '';
    if (!text.trim()) { skipped++; continue; }

    const classification = classify(text);
    if (!classification) { skipped++; continue; }

    const mappedTo = {
      category:   classification.category,
      value:      text,
      confidence: 0.65,  // imported memories start lower — needs real-world reinforcement
      task_scope: classification.task_scope ?? null,
      source:     'imported',
    };

    preview.push({ mem0_memory: text, mapped_to: mappedTo });

    if (!dry_run) {
      try {
        insert.run(
          `pref_${nanoid(10)}`,
          user_id,
          mappedTo.category,
          mappedTo.value,
          mappedTo.confidence,
          mappedTo.task_scope ? JSON.stringify(mappedTo.task_scope) : null,
        );
        created++;
      } catch {
        skipped++;
      }
    } else {
      created++; // "would have created"
    }
  }

  return {
    memories_fetched:      memories.length,
    preferences_created:   created,
    preferences_skipped:   skipped,
    dry_run,
    preview:               preview.slice(0, 5),
  };
}
