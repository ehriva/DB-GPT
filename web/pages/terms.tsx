import { Button, Checkbox, Form, Input, Modal, Popconfirm, Space, Table, Tag, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';

interface Source {
  title: string;
  url: string;
}
interface TermRecord {
  id: string;
  term: string;
  definition: string;
  formula: string;
  formula_confirmed: boolean;
  datasource: string;
  aliases: string[];
  sources: Source[];
  source_type: string;
  updated_at: string;
}
interface TermForm extends Omit<TermRecord, 'aliases' | 'sources' | 'id' | 'updated_at'> {
  aliases_text: string;
}

async function termRequest(path = '', options?: RequestInit) {
  const response = await fetch(`${process.env.API_BASE_URL ?? ''}/api/v1/chat/terms${path}`, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '共享术语操作失败，请重试。');
  return body;
}

export default function TermsPage() {
  const [records, setRecords] = useState<TermRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<TermRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<TermForm>();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRecords(await termRequest());
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const edit = (record: TermRecord | null, copy = false) => {
    setEditing(copy ? null : record);
    form.resetFields();
    form.setFieldsValue(
      record
        ? { ...record, aliases_text: record.aliases.join('，') }
        : {
            term: '',
            definition: '',
            formula: '',
            formula_confirmed: false,
            datasource: '',
            aliases_text: '',
            source_type: 'user',
          },
    );
    setOpen(true);
  };
  const save = async () => {
    try {
      const value = await form.validateFields();
      setSaving(true);
      const { aliases_text, ...rest } = value;
      await termRequest(editing ? `/${editing.id}` : '', {
        method: editing ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...rest,
          source_type: 'user',
          aliases: (aliases_text || '')
            .split(/[,，]/)
            .map(a => a.trim())
            .filter(Boolean),
          sources: editing?.sources || [],
        }),
      });
      setOpen(false);
      message.success('已保存，所有人下次均可选择。');
      await load();
    } catch (error) {
      if (error instanceof Error) message.error(error.message);
    } finally {
      setSaving(false);
    }
  };
  const remove = async (record: TermRecord) => {
    try {
      await termRequest(`/${record.id}`, { method: 'DELETE' });
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '删除失败');
    }
  };

  return (
    <main className='h-full overflow-auto p-6 dark:text-gray-100'>
      <h1 className='mb-2 text-2xl font-semibold'>共享术语库</h1>
      <p className='mb-5 text-gray-500'>
        所有人共用。解释明确时自动采用，存在歧义时再选择确认；修改或删除不改变已有报告的口径。
      </p>
      <Space className='mb-4' wrap>
        <Input.Search
          aria-label='搜索共享术语'
          placeholder='搜索名词、别名或解释'
          value={query}
          onChange={e => setQuery(e.target.value)}
          allowClear
        />
        <Button type='primary' onClick={() => edit(null)}>
          新增解释
        </Button>
        <Button onClick={() => void load()}>刷新</Button>
      </Space>
      <Table<TermRecord>
        rowKey='id'
        loading={loading}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 900 }}
        dataSource={records.filter(r =>
          [r.term, r.definition, ...r.aliases].join(' ').toLowerCase().includes(query.toLowerCase()),
        )}
        columns={[
          {
            title: '名词',
            dataIndex: 'term',
            render: (_, r) => (
              <>
                <b>{r.term}</b>
                <div>{r.aliases.join('、')}</div>
              </>
            ),
          },
          {
            title: '解释与口径',
            dataIndex: 'definition',
            render: (_, r) => (
              <>
                <div>{r.definition}</div>
                <div className='text-gray-500'>{r.formula?.trim() || '未提供计算公式'}</div>
                {r.formula?.trim() && (
                  <Tag color={r.formula_confirmed ? 'green' : 'orange'}>
                    {r.formula_confirmed ? '口径已确认' : '口径待确认'}
                  </Tag>
                )}
              </>
            ),
          },
          { title: '适用数据源', dataIndex: 'datasource', render: value => value || '通用' },
          {
            title: '来源',
            render: (_, r) => (
              <>
                <div>
                  {
                    (
                      {
                        user: '用户解释',
                        model: '模型理解（经用户确认，未核验）',
                        business: '业务资料',
                        history: '历史确认',
                      } as Record<string, string>
                    )[r.source_type]
                  }
                </div>
                {r.sources.map(s =>
                  /^https?:\/\//i.test(s.url) ? (
                    <div key={s.url}>
                      <a href={s.url} target='_blank' rel='noopener noreferrer'>
                        {s.title || s.url}
                      </a>
                    </div>
                  ) : null,
                )}
              </>
            ),
          },
          {
            title: '操作',
            render: (_, r) => (
              <Space wrap>
                <Button size='small' onClick={() => edit(r)}>
                  修改
                </Button>
                <Button size='small' onClick={() => edit(r, true)}>
                  另存解释
                </Button>
                <Popconfirm title='删除后所有人的候选列表都会移除此解释，是否删除？' onConfirm={() => remove(r)}>
                  <Button size='small' danger>
                    删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        title={editing ? '修改共享解释' : '新增共享解释'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void save()}
        confirmLoading={saving}
        okText='保存'
        cancelText='取消'
      >
        <Form form={form} layout='vertical'>
          <Form.Item name='term' label='名词' rules={[{ required: true, whitespace: true }]}>
            <Input maxLength={255} />
          </Form.Item>
          <Form.Item name='definition' label='中文解释' rules={[{ required: true, whitespace: true }]}>
            <Input.TextArea maxLength={4000} />
          </Form.Item>
          <Form.Item name='formula' label='计算口径（可选）'>
            <Input.TextArea maxLength={4000} />
          </Form.Item>
          <Form.Item name='formula_confirmed' valuePropName='checked'>
            <Checkbox>已确认计算口径</Checkbox>
          </Form.Item>
          <Form.Item name='datasource' label='适用数据源名称（留空表示通用）'>
            <Input maxLength={255} />
          </Form.Item>
          <Form.Item name='aliases_text' label='别名（逗号分隔）'>
            <Input />
          </Form.Item>
          <Form.Item name='source_type' hidden>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </main>
  );
}
