#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ErrorCode,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';

import { initDb } from './db.js';
import { getPreferences } from './tools/get-preferences.js';
import { logFeedback } from './tools/log-feedback.js';
import { upsertPreference } from './tools/upsert-preference.js';
import { explainRetrieval } from './tools/explain-retrieval.js';
import { importFromMem0 } from './tools/import-from-mem0.js';

// Initialise DB on startup
initDb();

const server = new Server(
  { name: 'coda-mcp', version: '0.1.0' },
  { capabilities: { tools: {} } },
);

// ── Tool definitions ────────────────────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'coda_get_preferences',
      description:
        'Retrieve the ranked preference slice most relevant to the current task. ' +
        'Call this at the start of any writing, coding, planning, or support response. ' +
        'Returns structured preference objects and a retrieval_id for later feedback logging.',
      inputSchema: {
        type: 'object',
        properties: {
          user_id:      { type: 'string', description: 'User identifier (default: "local")', default: 'local' },
          task_type:    { type: 'string', enum: ['writing','coding','planning','support','general'], description: 'Type of task being performed' },
          context_hint: { type: 'string', description: 'Optional brief description of the current request (improves future explain_retrieval output)' },
          limit:        { type: 'number', description: 'Max preferences to return (default: 5)', default: 5 },
        },
        required: ['task_type'],
      },
    },
    {
      name: 'coda_log_feedback',
      description:
        'Log a behavioral signal after delivering a response. ' +
        'Call this when the user accepts, edits, rejects, or re-requests an output. ' +
        'Coda uses these signals to sharpen confidence scores over time.',
      inputSchema: {
        type: 'object',
        properties: {
          user_id:                { type: 'string', default: 'local' },
          retrieval_id:           { type: 'string', description: 'The retrieval_id returned by coda_get_preferences for this interaction' },
          signal:                 { type: 'string', enum: ['accepted','edited','rejected','reused'] },
          task_type:              { type: 'string', description: 'Task type of the interaction' },
          preference_ids_in_play: { type: 'array', items: { type: 'string' }, description: 'Preference IDs that were used in this response', default: [] },
          edit_delta:             { type: 'string', description: 'What the user changed (for "edited" signals)' },
        },
        required: ['signal'],
      },
    },
    {
      name: 'coda_upsert_preference',
      description:
        'Create or update a preference object. ' +
        'Call this when the user explicitly states a preference ("always use bullet points", "I\'m a senior engineer"). ' +
        'Explicit preferences get high confidence (0.9) by default.',
      inputSchema: {
        type: 'object',
        properties: {
          user_id:    { type: 'string', default: 'local' },
          category:   { type: 'string', enum: ['tone','format','goals','constraints','habits','role'] },
          value:      { type: 'string', description: 'The preference statement' },
          task_scope: { type: 'array', items: { type: 'string', enum: ['writing','coding','planning','support','general'] }, description: 'Which task types this applies to. Omit for all tasks.' },
          confidence: { type: 'number', minimum: 0, maximum: 1, default: 0.9, description: 'Confidence score (0–1). Defaults to 0.9 for explicit preferences.' },
          pinned:     { type: 'boolean', default: false, description: 'Pinned preferences are never modified by behavioral signals.' },
          source:     { type: 'string', enum: ['explicit','inferred','imported'], default: 'explicit' },
        },
        required: ['category', 'value'],
      },
    },
    {
      name: 'coda_explain_retrieval',
      description:
        'Explain why specific preferences were (or were not) returned for a given retrieval. ' +
        'Powers the "why this?" feature in the inspection console.',
      inputSchema: {
        type: 'object',
        properties: {
          retrieval_id: { type: 'string', description: 'The retrieval_id from a previous coda_get_preferences call' },
        },
        required: ['retrieval_id'],
      },
    },
    {
      name: 'coda_import_from_mem0',
      description:
        'Import and structure raw memories from a Mem0 account into Coda preference objects. ' +
        'Use dry_run: true to preview what would be imported without writing to the database.',
      inputSchema: {
        type: 'object',
        properties: {
          user_id:      { type: 'string', default: 'local' },
          mem0_user_id: { type: 'string', description: 'Your Mem0 user ID' },
          mem0_api_key: { type: 'string', description: 'Your Mem0 API key' },
          dry_run:      { type: 'boolean', default: false, description: 'Preview without writing to DB' },
        },
        required: ['mem0_user_id', 'mem0_api_key'],
      },
    },
  ],
}));

// ── Tool dispatch ───────────────────────────────────────────────────────────

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args = {} } = request.params;

  try {
    let result: unknown;

    switch (name) {
      case 'coda_get_preferences':
        result = getPreferences(args as Parameters<typeof getPreferences>[0]);
        break;
      case 'coda_log_feedback':
        result = logFeedback(args as Parameters<typeof logFeedback>[0]);
        break;
      case 'coda_upsert_preference':
        result = upsertPreference(args as Parameters<typeof upsertPreference>[0]);
        break;
      case 'coda_explain_retrieval':
        result = explainRetrieval(args as Parameters<typeof explainRetrieval>[0]);
        break;
      case 'coda_import_from_mem0':
        result = await importFromMem0(args as Parameters<typeof importFromMem0>[0]);
        break;
      default:
        throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
    }

    return {
      content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
    };

  } catch (err) {
    if (err instanceof McpError) throw err;

    // Zod validation errors → user-facing message
    const message = err instanceof Error ? err.message : String(err);
    return {
      content: [{ type: 'text', text: `Error: ${message}` }],
      isError: true,
    };
  }
});

// ── Start ───────────────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Note: never write to stdout after this point — it belongs to the MCP protocol.
  process.stderr.write('Coda MCP server running on stdio\n');
}

main().catch((err) => {
  process.stderr.write(`Fatal: ${err}\n`);
  process.exit(1);
});
