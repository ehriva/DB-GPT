import assert from 'node:assert/strict';
import test from 'node:test';
import { historyDatabase, matchHistoryDatabase } from './history-datasource';

const sources = [{ db_name: 'monthly', db_type: 'sqlite' }];

test('structured name and type restore exact source and explicit null stops fallback', () => {
  const db = historyDatabase({ resource_context: { database: { name: 'monthly', type: 'sqlite' } } }, '');
  assert.deepEqual(matchHistoryDatabase(db, sources), { status: 'matched', database: sources[0] });
  assert.deepEqual(historyDatabase({ resource_context: { database: null } }, '[Database: monthly] query'), null);
  assert.deepEqual(matchHistoryDatabase(null, sources), { status: 'cleared', database: null });
});

test('legacy only restores a standard prefix with a unique source', () => {
  const db = historyDatabase({}, '[Database: monthly]\nquery');
  assert.equal(matchHistoryDatabase(db, sources).status, 'matched');
  assert.equal(matchHistoryDatabase(db, [...sources, { db_name: 'monthly', db_type: 'mysql' }]).status, 'missing');
  assert.equal(historyDatabase({}, '说明使用 [Database: monthly]'), undefined);
});

test('loading, deleted sources and type conflicts do not select another database', () => {
  const db = { name: 'monthly', type: 'sqlite' };
  assert.equal(matchHistoryDatabase(db, undefined).status, 'loading');
  assert.equal(matchHistoryDatabase(db, []).status, 'missing');
  assert.equal(matchHistoryDatabase(db, [{ db_name: 'monthly', db_type: 'mysql' }]).status, 'missing');
  assert.equal(matchHistoryDatabase(undefined, sources).status, 'absent');
});
