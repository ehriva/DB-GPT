// Copy to an isolated Next.js pages directory for browser regression checks.
// This fixture is not a production route and makes no model requests.
import React, { useRef, useState } from 'react';
import ManusLeftPanel from '../new-components/chat/content/ManusLeftPanel';
import QuestionDock, { type QuestionRequest } from '../new-components/chat/content/QuestionDock';
import MarkdownContext from '../new-components/common/MarkdownContext';
import MarkdownErrorBoundary from '../new-components/common/MarkdownErrorBoundary';
import type { TaskResult } from '../utils/react-agent-final';

function TestRenderer({ blocked, text }: { blocked: React.MutableRefObject<boolean>; text: string }) {
  if (blocked.current) throw new Error('Reply recovery regression fixture');
  return <MarkdownContext>{text}</MarkdownContext>;
}

export default function ReplyRecoveryFixture() {
  const blocked = useRef(false);
  const [, update] = useState(0);
  const [text, setText] = useState('**你好** 😊\n<strong>原文标签</strong>');
  const [scenario, setScenario] = useState('succeeded');
  const [navigation, setNavigation] = useState(0);
  const [questionRequest, setQuestionRequest] = useState<QuestionRequest | null>(null);
  const [answer, setAnswer] = useState('');
  const taskResult: TaskResult | undefined = ['succeeded', 'incomplete', 'failed', 'cancelled'].includes(scenario)
    ? { status: scenario as TaskResult['status'], reason: 'fixture', missing: [] }
    : undefined;

  return (
    <div className='p-4 space-y-3'>
      <h1>回复恢复回归验证</h1>
      <input aria-label='保留的输入框' />
      <button onClick={() => setNavigation(n => n + 1)}>历史导航验证 {navigation}</button>
      <button
        onClick={() => {
          blocked.current = true;
          update(n => n + 1);
        }}
      >
        触发回复渲染异常
      </button>
      <button
        onClick={() => {
          blocked.current = false;
        }}
      >
        修复测试渲染器
      </button>
      <button
        onClick={() => {
          blocked.current = false;
          setText('新回复正常显示');
        }}
      >
        切换回复
      </button>
      <button
        onClick={() =>
          setQuestionRequest({
            request_id: 'no-options',
            conv_id: 'fixture',
            questions: [{ question: '没有选项的问题，请输入回答。' }],
          } as QuestionRequest)
        }
      >
        无选项问题
      </button>
      <button
        onClick={() =>
          setQuestionRequest({
            request_id: 'next-question',
            conv_id: 'fixture',
            questions: [
              { question: '第二个问题', header: '', options: [{ label: '选择 A', description: '' }] },
              { question: '第三个问题', header: '', options: [{ label: '选择 B', description: '' }] },
            ],
          })
        }
      >
        切换问题请求
      </button>
      {questionRequest && (
        <QuestionDock
          key={questionRequest.request_id}
          request={questionRequest}
          onReply={(id, answers) => {
            setAnswer(JSON.stringify({ id, answers }));
            setQuestionRequest(null);
          }}
          onReject={() => setQuestionRequest(null)}
        />
      )}
      <div aria-label='问题回答结果'>{answer}</div>
      <MarkdownErrorBoundary key={text} text={text} errorLabel='回复格式显示失败，已显示原文。' retryLabel='重试显示'>
        <TestRenderer blocked={blocked} text={text} />
      </MarkdownErrorBoundary>
      <label>
        任务状态
        <select aria-label='任务状态' value={scenario} onChange={event => setScenario(event.target.value)}>
          {['idle', 'working', 'legacy', 'succeeded', 'incomplete', 'failed', 'cancelled'].map(value => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <ManusLeftPanel
        sections={[]}
        isWorking={scenario === 'working'}
        taskResult={taskResult}
        assistantText={scenario === 'idle' || scenario === 'working' ? '' : '你好！普通回复正常显示。'}
        userQuery='你好'
      />
    </div>
  );
}
