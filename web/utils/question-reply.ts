export interface QuestionReplyDetails {
  conv_id: string;
  decisions?: {
    save_term?: boolean;
    formula?: string;
    formula_confirmed?: boolean;
    aliases?: string[];
  };
}

export async function submitQuestionReply(requestId: string, answers: string[][], details?: QuestionReplyDetails) {
  const response = await fetch(`${process.env.API_BASE_URL ?? ''}/api/v1/chat/question/${requestId}/reply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers, ...details }),
  });
  const body = await response.json();
  if (!response.ok || body.success === false) {
    throw new Error(body.detail || body.err_msg || '回答提交失败，请重试。');
  }
}

export async function submitQuestionReject(requestId: string, convId?: string) {
  const query = convId ? `?conv_id=${encodeURIComponent(convId)}` : '';
  const response = await fetch(`${process.env.API_BASE_URL ?? ''}/api/v1/chat/question/${requestId}/reject${query}`, {
    method: 'POST',
  });
  const body = await response.json();
  if (!response.ok || body.success === false) {
    throw new Error(body.detail || body.err_msg || '取消提交失败，请重试。');
  }
}
