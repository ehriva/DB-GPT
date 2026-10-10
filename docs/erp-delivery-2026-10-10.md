# DB-ERP 修复交付说明

交付日期：2026-10-10（Asia/Taipei）。本轮软件修复与约定验收已完成，Goal `muzvhe65-5hpvoy` 的 9 项任务全部完成，独立完成审计于本地 02:41 批准，随后完成归档。当前没有待审计门槛。

## 已验收版本

| 项目 | 已核对值 |
| --- | --- |
| 服务入口 | http://localhost:5670/ |
| 容器 | db-erp-erp-1 |
| 当前镜像标签 | dbgpt-erp:product-total-fix-20261010 |
| 当前镜像 | sha256:0412be10e9d9567a89dd7f065b7cf899cc304c7eba8f5bf5cdd152ed1c98fa99 |
| 回滚镜像标签 | dbgpt-erp:report-state-repair-20261009-033701 |
| 回滚镜像 | sha256:8ebee4872ab8a5184fa673aff82f876ea1317d5058bf256aaa7cf60d583ed888 |
| 应用模型 | LongCat-2.5-Preview；DeepSeek 禁用 |
| 本次交付时只读运行检查 | running/healthy；回滚镜像存在；模型列表仅 LongCat |

运行镜像、回滚和模型本次均重新只读核对，未重建或重启服务。任务文件、业务库、历史、共享术语和挂载保留。未提交或推送 Git。

后续补丁（2026-10-10）：按用户要求修复产品列存在时合法合计行被误拒的问题，已构建并部署单模块补丁，当前版本见上表。修复前4个失败回归、修复后API目录276项通过，部署后同输入误拒消失；配置和挂载保留。此次补丁的直接回滚镜像为 `dbgpt-erp:report-state-repair-20261009-033701`；使用现有两个Compose文件再加 `outputs/product-total-fix/compose.delta.yml` 启动。详情见 `outputs/product-total-fix/repair-note.md`。前述“未重建或重启”是原收口检查，不适用于本次补丁发布。

## 验收范围和证据入口

- 正常月度报告、全量与完整成本子集、客户/月范围、真实销售身份和数值，以及多轮排版、刷新、历史重开与实际重跑：`outputs/pi-acceptance/handoff-for-codex.md`，其中历史失败/取消及排版未生效样本继续保留。
- 完整数值要求下的合法 HTML 交付与正确未完成终态、纯解释、均价及真实重执行、共享术语复用、取消、HTML/SVG 三态：`outputs/require-value-delivery/evidence-index.md`。
- 独立逐 case 复核：`outputs/require-value-delivery/step-a-verification/step-a-verdict.md`。
- 独立复审与不确定项收口：`outputs/require-value-delivery/step-c-review/reviewer-verdict.md`、`parent-closure-of-uncertains.json`。
- 完成审计原始记录：`.pi/goals/goal_events.jsonl` 中对应 Goal 的 `audit_result`、`goal_completed`、`goal_archived`；归档 `.pi/goals/archived/goal_2026101002410521_muzvhe65-5hpvoy.md`。
- 回归口径：先前跨包十套件 276 项通过；最终渲染修复的聚焦报告测试 13 项及 API v1 目录 256 项通过。它们是不同测试集合，不代表全仓全部检查通过。

本轮仅本机验收，未声明第二台电脑、多用户权限或公网生产部署已通过。

## 业务数据限制

指定客户武汉钧恒科技有限公司（LTS0081）的 2026.01、2026.06、2026.07 共 51 条销售。47 条成本输入完整；4 条缺工费，涉及 2 个产品。另有 10 条真实零工费记录，零是有效值。

| 期间 | 全量销售额（元） | 缺工费销售行数 | 全量毛利额/率 |
| --- | ---: | ---: | --- |
| 2026.01 | 68,672.56 | 1 | NULL |
| 2026.06 | 86,436.69 | 2 | NULL |
| 2026.07 | 796,306.17 | 1 | NULL |

缺失不能填零；完整成本子集不能替代全量。普通分析可以带数据警告交付。明确要求全量完整数值时，报告交付可用销售额和实际缺失说明，并保持 `incomplete/missing_deliverables`，这是正确业务结果，不表示软件修复仍未完成。

已验收的完整数值场景：
http://127.0.0.1:5670/?id=98a574f6-6cef-4c49-8561-57349fd9efb5

实际报告：`outputs/require-value-delivery/renderer-repair/ui-retest/case0-report_f0051a70e81d.html`。

## 小范围只读试用

1. 使用 localhost，在新任务里明确选择数据源，输入真实业务客户和期间，说明需要哪些指标及是否生成报告。
2. 普通需求按已有 NULL/警告口径执行；只有业务确实要求完整数值时才提出硬性要求。看到报告“未完成”时先读数据限制。
3. 保留任务链接、报告、采用口径和限制，人工核对后再用于业务判断。新需求出现异常时记录其原文和真实终态；不反复重试挑成功结果，也不改写历史报告。
4. 交付收口没有重新提交模型业务任务。试用从下一条真实业务需求开始，既有验收用例无需从头再跑。

## 如需补齐工费

| 产品编码 | 受影响期间 | 原销售 rowid | 待核对输入 |
| --- | --- | --- | --- |
| 102020200005 | 2026.01、2026.06 | 102、1388 | 工费及其适用依据 |
| 102010200014 | 2026.06、2026.07 | 1372、1722 | 工费及其适用依据 |

由业务负责人确认实际工费、单位和适用口径，先在数据库副本验证，再另行明确授权数据变更。此次没有补数或修改原业务库。

## 回滚与保留

需要回滚时，先保留当前镜像和任务文件，确认无运行任务，核对现有 Compose 与挂载，再使用上述保留镜像恢复应用版本。模型凭据通过现有环境配置提供，不复制进报告或命令。回滚不负责补齐工费，也不删除业务数据、历史、术语或卷；禁止 `docker compose down -v` 和无范围的数据清理。

根目录 `repair_report.md`、`task_plan.md`、`notes.md` 及 completion-qa/Walmart 材料属于 10 月 2 日旧任务；明确标注为历史的段落、原始日志、报告、截图和审计否决不改写。当前交付状态以本说明及已归档 Goal 为入口。
