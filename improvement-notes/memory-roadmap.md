# nanobot Memory Roadmap (Rewrite)

更新时间：2026-02-28  
目的：基于当前代码真实实现，形成后续 memory 重构的执行蓝图，避免文档与代码状态脱节。

## 1. 目标与范围

本路线图聚焦 `nanobot` 的记忆管理主链：

- 运行时记忆写入：`nanobot/agent/memory.py`
- 运行时记忆读取注入：`nanobot/agent/context.py`
- 会话触发与 consolidation 调度：`nanobot/agent/loop.py`
- 运维清理与观测：`nanobot/agent/memory_maintenance.py`、`nanobot/cli/commands.py` (`memory-audit` / `memory-observe`)

不在本次主线范围：

- 向量检索/RAG 系统
- 跨项目统一 memory 平台
- 多租户权限系统

## 2. 当前实现基线（Code Truth）

### 2.1 存储分层（已落地）

- `memory/MEMORY.md`：长期记忆（全文替换写入）
- `memory/HISTORY.md`：流水归档（append）
- `memory/YYYY-MM-DD.md`：daily 分层（Topics/Decisions/Tool Activity/Open Questions）
- `memory/archive/`：归档后的 daily 文件
- `memory/observability/*.jsonl`：清理、路由、guard、sanitize、冲突、TTL 等指标

### 2.2 写入链路（已落地）

`MemoryStore.consolidate()` 已具备：

- scope 计算与 snapshot 边界处理
- chunk 化 consolidation（含 context overflow 缩块重试）
- `save_memory` tool call 解析
- daily 路由策略：
  - `compatible`
  - `preferred`（缺少 `daily_sections` 触发一次强化重试）
  - `required`（禁止非结构化 fallback）
- `memory_update` 清洗与 guard 拦截（长度、结构、重复、日期行、URL 行等）
- guard/sanitize/conflict 的 observability 指标写入

### 2.3 读取链路（已落地）

`ContextBuilder` 已支持：

- 默认只注入长期记忆
- 仅在回顾型 query 时按需注入 recent daily（默认排除 Tool Activity）
- recent daily 注入包含 `date + section` 锚点
- context trace 写入（`before_compact/after_compact/before_send`）

### 2.4 生命周期与运维（已落地）

`memory-audit` 已支持：

- `--apply`、`--apply-safe`
- `--archive-dry-run`、`--archive-apply`
- `--archive-compact-apply`（归档前压缩回流 HISTORY）
- `--daily-ttl-dry-run`、`--daily-ttl-apply`（仅针对 `memory/archive`）
- 多类 summary 与 markdown 导出

`memory-observe` 已支持一键快照与 dashboard 聚合输出。

## 3. 现存主要问题（重构驱动）

### 3.1 责任聚合过重

`memory.py` 同时承担：

- consolidation 调度
- prompt 构建
- tool call 解析
- daily 路由
- 长期记忆清洗/guard
- 指标写入

问题：改动耦合高，单点回归成本高。

### 3.2 策略与执行耦合

- `daily_sections_mode`、sanitize、guard、fallback 逻辑散落在流程代码中。
- 缺少统一的“策略决策面”（policy engine）。

问题：难以灰度新策略，难以解释策略冲突优先级。

### 3.3 数据模型仍偏文本态

- 长期记忆核心仍是 markdown 全文替换。
- 缺少稳定条目 ID/版本语义（当前偏日志证据，不是实体模型）。

问题：难做精细更新、冲突解决和可逆回滚。

### 3.4 生命周期链路可观测但可逆性不完整

- 已有 archive/compact/ttl 审计日志。
- 但缺少“按 run_id/metrics 恢复”的标准回滚能力。

问题：自动化清理策略不易扩大灰度范围。

### 3.5 指标很多，但缺统一 SLO 门槛

- 已有 routing/guard/sanitize/cleanup 大量指标。
- 但缺明确 release gate（例如何时从 compatible 升 preferred）。

问题：决策依赖人工体感，自动化不足。

## 4. 重构目标（2026Q1）

1. **模块化**：把 memory 主链拆成可独立测试的组件。
2. **策略化**：把 routing/guard/lifecycle 变成显式 policy。
3. **可逆性**：所有 destructive 操作支持 run-level 恢复。
4. **可运营**：建立可执行的 SLO 与 rollout 门槛。
5. **兼容迁移**：不破坏现有 workspace 数据结构。

## 5. 目标架构（Target Shape）

### 5.1 逻辑分层

- `memory_domain.py`
  - 结构定义：HistoryEntry、DailySections、MemoryUpdateCandidate、PolicyDecision
- `memory_consolidation.py`
  - chunk/scope/retry/tool-call orchestration
- `memory_routing_policy.py`
  - `daily_sections_mode`、fallback 决策、优先级裁决
- `memory_guard_policy.py`
  - sanitize + guard + conflict rules
- `memory_lifecycle.py`
  - cleanup/archive/compact/ttl/restore
- `memory_observability.py`
  - 指标 schema、append、summary 辅助

`memory.py` 最终仅作为 facade（薄入口）。

### 5.2 策略优先级（统一裁决）

统一采用：

`data safety > append-only traceability > interruption safety > recall quality > token cost`

说明：

- 任何策略优化不得突破 data safety（例如禁止静默覆盖 MEMORY）。
- recall 优化与 token 预算冲突时，优先保安全与可追溯。

## 6. 分阶段实施计划

### Phase R0：基线冻结（短周期）

目标：确保后续重构有可比较基线。

- 固化样本集：
  - 正常对话写入
  - 工具高频会话
  - 缺失/错误 `daily_sections`
  - `/new` 触发 consolidation 场景
- 固化指标快照模板（routing + guard + sanitize + cleanup + ttl）

DoD：

- 有一份可重复执行的 baseline test + observe 命令集。

### Phase R1：模块拆分（行为不变）

目标：仅重构结构，不变业务行为。

- 抽离 consolidation orchestration
- 抽离 routing policy
- 抽离 guard policy
- 保持 CLI 行为与指标字段兼容

DoD：

- 现有 memory 相关测试全通过
- 关键 golden case 输出一致（history/daily/memory）

### Phase R2：策略面收敛

目标：把策略开关与策略结果显式化。

- 完整落地 `compatible/preferred/required` 语义文档与指标
- 给 `preferred` 增加明确收益指标门槛
- 引入 P0/P1/P2 最小打标（先打在 conversion/compact/ttl 指标层，不改存储格式）

DoD：

- `preferred` 升级决策可由指标自动判定（见 Phase Gate）
- 打标数据可用于 cleanup/ttl 报告分层统计

### Phase R3：生命周期闭环

目标：形成可灰度、可回滚的生命周期操作。

- `archive-compact` 与 `ttl` 增加 run_id 级恢复入口
- 增加 `restore` CLI（按 run_id/file list 回滚）
- 增加 destructive 操作前置风险摘要标准化输出

DoD：

- 任一 apply 都可基于日志恢复到 apply 前状态

### Phase R4：读取策略与 recall 质量

目标：提升 recall 命中率并控制 token 成本。

- 收敛 recall query 判定规则
- 对 recent daily 注入做 section/age/token 三维预算
- 在 dashboard 输出 recall 命中代理指标

DoD：

- 回顾类 query 的成功率提升，默认 token 开销不显著上升

### Phase R5：数据模型升级（谨慎）

目标：在不破坏 markdown 兼容的前提下引入结构化元数据。

- 为 daily/history 引入轻量元信息（id/source/run/tag）
- MEMORY 仍可保持 markdown 主体，但引入可解析边注格式

DoD：

- 保持旧数据可读可写
- 新旧版本共存下行为可预测

## 7. Phase Gate（升级门槛）

从 `compatible -> preferred` 的建议门槛（7 天窗口）：

- `structured_daily_ok_rate >= 75%`
- `fallback_missing_rate` 连续下降
- `preferred_retry_used` 占比不高且有效提升 `tool_call_has_daily_sections`
- guard/sanitize 不出现明显回归

从 `preferred -> required` 的建议门槛（14 天窗口）：

- `tool_call_has_daily_sections_rate >= 95%`
- required 灰度会话中无明显 recall 退化
- fallback 相关告警可控

## 8. 测试策略

- 单元测试：
  - policy 决策函数（表驱动）
  - guard/sanitize 边界样本
  - lifecycle/ttl/restore 数据路径
- 集成测试：
  - consolidate 端到端（tool call 字符串/对象参数、错误返回）
  - memory-audit CLI 关键命令组合
- 回归测试：
  - 基线样本 golden output 比对

## 9. 运维与配置建议

建议默认值（现阶段）：

- `memory_daily_sections_mode = compatible`
- 日常运维优先：
  - `memory-observe`
  - `memory-audit --apply-safe`
  - `memory-audit --archive-compact-apply`（分批）
  - `memory-audit --daily-ttl-dry-run` 后再 `--daily-ttl-apply`

## 10. 待决策事项（Open Decisions）

1. P0/P1/P2 是否先仅用于观测层，还是直接参与 TTL 删除策略。  
2. restore 机制采用“备份目录优先”还是“metrics 重放优先”。  
3. `required` 模式是否按 provider/model 白名单启用。  
4. 是否在 R5 前引入 `L1 insights/lessons` 作为轻量中间层。  

## 11. 本文档维护规则

- 文档只保留“当前状态 + 可执行计划”，不再累积冗长流水日志。  
- 每个阶段完成后仅更新：
  - 状态
  - 关键行为变更
  - 指标与 gate 结论
  - 下一阶段决策
