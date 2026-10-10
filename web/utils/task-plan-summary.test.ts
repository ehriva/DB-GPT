import assert from 'node:assert/strict';
import { test } from 'node:test';
import { summarizeTaskPlan } from './task-plan-summary';

test('cancelled delivery remains in the denominator and cannot look completed', () => {
  const result = summarizeTaskPlan([{ status: 'completed' }, { status: 'cancelled' }, { status: 'cancelled' }]);
  assert.equal(result.total, 3);
  assert.equal(result.done, 1);
  assert.equal(result.cancelled, 2);
  assert.equal(result.allDone, false);
});

test('complete, empty and user-cancelled plans keep distinct summaries', () => {
  assert.equal(summarizeTaskPlan([{ status: 'completed' }]).allDone, true);
  assert.equal(summarizeTaskPlan([]).allDone, false);
  assert.equal(summarizeTaskPlan([{ status: 'cancelled' }]).allDone, false);
});
