import assert from 'node:assert/strict';
import test from 'node:test';

test('structured restored datasource carries type and explicit clear suppresses legacy marker', () => {
  const restored = getRerunPayload({ context: '[Database: monthly] query',
    attachedDb: { db_name: 'monthly', db_type: 'sqlite' } }, 'model');
  assert.equal(restored.ext_info?.database_type, 'sqlite');
  const cleared = getRerunPayload({ context: '[Database: monthly] query', historyDatabase: null }, 'model');
  assert.equal(cleared.ext_info?.database_name, undefined);
});

import { getRerunPayload } from './chat-rerun';

test('replays the selected round with its original model and context', () => {
  const original = {
    user_input: '[Database: original_db] original question',
    model_name: 'original-model',
    ext_info: { database_name: 'original_db', skill_id: 'sql', connector_ids: ['original-connector'] },
  };
  assert.deepEqual(
    getRerunPayload({ context: 'original question', replayPayload: original }, 'different-model'),
    original,
  );
});

test('preserves the legacy example file payload', () => {
  const original = { user_input: 'analyze example', ext_info: { file_path: '/examples/demo.csv' } };
  assert.deepEqual(getRerunPayload({ context: 'analyze example', replayPayload: original }, 'model'), original);
});

test('restored rounds retain their prefix and their own file IDs', () => {
  const files = [{ file_id: 'file-for-selected-round', name: 'original.csv' }];
  const result = getRerunPayload(
    { context: '[Database: original_db] question', model_name: 'history-model', attachedFiles: files },
    'current-model',
  );
  assert.equal(result.user_input, '[Database: original_db] question');
  assert.equal(result.model_name, 'history-model');
  assert.deepEqual(result.ext_info, {
    database_name: 'original_db',
    file_ids: ['file-for-selected-round'],
    input_files: files,
  });
});

test('plain history does not attach composer context', () => {
  const result = getRerunPayload({ context: 'old question' }, 'model');
  assert.equal(result.user_input, 'old question');
  assert.deepEqual(result.ext_info, {});
});

test('restores knowledge selection only from the stored context prefix', () => {
  assert.deepEqual(
    getRerunPayload({ context: '[Database: db] [Knowledge: docs] question' }, 'model').ext_info,
    { database_name: 'db', knowledge_space_name: 'docs' },
  );
  assert.deepEqual(getRerunPayload({ context: 'explain [Database: example]' }, 'model').ext_info, {});
});
