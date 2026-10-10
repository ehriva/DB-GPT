import assert from 'node:assert/strict';
import { test } from 'node:test';
import { submitQuestionReject, submitQuestionReply } from './question-reply';

test('confirmation sends conversation and save choice, rejects failed envelopes', async () => {
  const original = global.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  global.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify({ success: true }), { status: 200 });
  };
  try {
    await submitQuestionReply('que_one', [['定义']], { conv_id: 'conv', decisions: { save_term: false } });
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
      answers: [['定义']], conv_id: 'conv', decisions: { save_term: false },
    });
    await submitQuestionReject('que_one', 'conv');
    assert.match(calls[1].url, /reject\?conv_id=conv$/);
    global.fetch = async () => new Response(JSON.stringify({ success: false, err_msg: '请求已过期' }), { status: 200 });
    await assert.rejects(submitQuestionReply('old', [['定义']]), /请求已过期/);
    global.fetch = async () => new Response(JSON.stringify({ detail: '会话不匹配' }), { status: 409 });
    await assert.rejects(submitQuestionReject('wrong', 'conv'), /会话不匹配/);
  } finally { global.fetch = original; }
});
