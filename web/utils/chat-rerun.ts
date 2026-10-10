import type { ChatReplayPayload } from '../types/scheduled-task';

interface RerunMessage {
  context: string;
  model_name?: string;
  replayPayload?: ChatReplayPayload;
  attachedFiles?: readonly { file_id: string }[];
  attachedDb?: { db_name: string; db_type: string };
  historyDatabase?: { name: string; type?: string } | null;
}

export function getRerunPayload(message: RerunMessage, fallbackModel: string): ChatReplayPayload {
  if (message.replayPayload) return message.replayPayload;

  // Restored human messages already contain their database/knowledge prefix.
  const files = message.attachedFiles ?? [];
  const prefix = message.context.match(/^(?:\[(?:Database|Knowledge): [^\]]+\]\s*)+/)?.[0] ?? '';
  const databaseName = message.historyDatabase === null ? undefined
    : message.historyDatabase?.name || prefix.match(/\[Database: ([^\]]+)\]/)?.[1];
  const knowledgeName = prefix.match(/\[Knowledge: ([^\]]+)\]/)?.[1];
  return {
    version: files.length > 0 ? 2 : 1,
    user_input: message.context,
    chat_mode: 'chat_react_agent',
    model_name: message.model_name || fallbackModel,
    select_param: '',
    temperature: 0.6,
    max_new_tokens: 4000,
    ext_info: {
      ...(message.historyDatabase === null ? {} : message.attachedDb ? { database_name: message.attachedDb.db_name,
        database_type: message.attachedDb.db_type } : databaseName ? { database_name: databaseName } : {}),
      ...(knowledgeName ? { knowledge_space_name: knowledgeName } : {}),
      ...(files.length > 0 ? { file_ids: files.map(file => file.file_id), input_files: files } : {}),
    },
  };
}
