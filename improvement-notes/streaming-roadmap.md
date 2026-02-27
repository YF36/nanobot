# nanobot 流式输出路线图（Streaming Roadmap）

本文件聚焦 nanobot 的“真正流式输出（token/delta streaming）”改造方案，目标是在尽量不破坏现有架构的前提下，先打通端到端流式文本输出，再逐步演进事件模型与渠道渲染体验。

适用范围（当前设计）：

- 优先支持 LLM 文本增量（`text delta`）流式输出
- 先覆盖支持 SSE/streaming 的 provider（优先 `OpenAICodexProvider`）
- 先复用现有 progress 消息机制，不要求渠道支持“编辑同一条消息”

---

## 当前落地状态（截至 2026-02-27）

- `S-Stream1`：已落地（最小版）
  - provider 新增可选 `stream_chat()` 抽象并接入主执行链路
  - `OpenAICodexProvider` 已支持文本增量流式输出
  - `TurnRunner` 已支持流式消费与回退路径
- `S-Stream2`：已落地（最小版）
  - 内部事件模型已新增 `message_delta`
  - turn summary 已增加流式相关统计字段（如 delta 次数/字符数）
- `S-Stream3`：部分落地（最小版）
  - 已支持 progress 消息限流上限（每 turn 最大消息数）
  - 已支持可选“流式结束标记”消息（默认关闭）
  - 渠道“单条消息编辑式流式”尚未落地

代表性提交（streaming 主线）：

- `9c38a78`：S-Stream1 provider/runner 最小流式闭环
- `fc92176`：流式 progress flush 行为可配置
- `b808dde`：引入 `message_delta` 事件
- `30c6558`：summary 增加 message delta 统计
- `2538aa9`：每 turn progress 消息上限
- `6f9d791`：可选 stream done marker

---

## 当前状态（基线）

### 已有能力（可复用）

1. `TurnRunner` 已有进度回调接口 `on_progress(...)`
- 当前主要用于：
  - tool call 前的文本片段提示
  - tool hint（如 `read_file(...)`）
- 但不是 token 级流式输出。

2. `MessageProcessor` / `ProgressPublisher` / `ChannelManager` 已有 progress 消息链路
- 可通过 `_progress` metadata 下发到渠道
- 受 `send_progress` / `send_tool_hints` 配置控制

3. 内部事件模型已存在（turn/tool 事件）
- `turn_id / sequence / timestamp_ms / source`
- 已有 capabilities manifest 与 `/health?debug=events`

4. `OpenAICodexProvider` 底层已经在使用 SSE
- 已有 `_iter_sse()` / `_consume_sse()` 解析逻辑
- 当前只是 provider 内部“先流式接收，再聚合成完整 `LLMResponse` 返回”

### 当前限制（问题）

1. `LLMProvider` 接口只有 `chat(...) -> LLMResponse`
- 没有统一的 `stream_chat(...)` 抽象

2. `TurnRunner` 是“等待完整 provider 响应后继续”
- 无法在模型生成过程中把 token/delta 实时推送出去

3. 渠道层主要是“发消息”，不是“编辑消息”
- 第一阶段可以接受（先用 progress 多条消息）

---

## 设计目标（最小版）

### 目标（第一阶段）

- 在不移除现有 `chat()` 路径的前提下，新增可选流式接口
- `TurnRunner` 能优先使用流式 provider 消费文本 delta
- 渠道端用户能看到“边生成边输出”的效果（即便是 progress 多条消息）
- 非流式 provider 行为完全不变

### 非目标（第一阶段不做）

- 不要求所有 provider 同时实现流式
- 不要求工具调用参数也做增量流式（tool args delta）
- 不要求所有渠道支持“消息编辑式流式 UI”
- 不重构现有重试体系为完整流式状态机（先兼容回退）

---

## 总体方案（双路径兼容）

核心思路：

- 保留现有 `chat()`（稳定兼容路径）
- 新增可选 `stream_chat()`（流式增强路径）
- `TurnRunner` 按 provider 能力选择路径

### 双路径执行策略

1. provider 支持 `stream_chat()`：
- 走流式路径（增量 delta -> progress / 事件）
- 最终仍产出完整 `LLMResponse` 供后续工具循环复用

2. provider 不支持 `stream_chat()`：
- 继续走现有 `chat()` 路径（无行为变化）

这样可以实现：

- “先打一条 provider（如 Codex）”
- “其余 provider 暂不动”
- “最小风险上线”

---

## Phase S-Stream1（最小可用，已落地）

### S-Stream1 目标（状态：已落地，最小版）

- 新增 provider 流式抽象（可选）
- `OpenAICodexProvider` 实现 `stream_chat()`
- `TurnRunner` 支持流式消费文本 delta
- 继续复用 progress 消息机制（建议加节流）

### 1. Provider 层：新增可选 `stream_chat()` 接口

建议在 `LLMProvider` 抽象层新增可选方法（默认可抛 `NotImplementedError` 或返回 `None` 能力）：

- `stream_chat(...) -> AsyncIterator[LLMStreamEvent]`

建议新增流式事件类型（最小集合）：

- `text_delta`
- `tool_calls`（可一次性发出，不要求增量）
- `done`
- `error`

#### 建议的事件语义（最小版）

1. `text_delta`
- 表示模型新增了一段文本内容
- 字段建议：
  - `delta`

2. `tool_calls`
- 表示该次响应包含工具调用（可在 provider 内聚合后一次性交给上层）
- 字段建议：
  - `tool_calls`（最终结构，复用现有 `ToolCallRequest`）
  - `content`（若 provider 同时已有部分文本）
  - `finish_reason`（可选）

3. `done`
- 表示响应结束
- 字段建议：
  - `response`（完整 `LLMResponse`）

4. `error`
- 流式请求失败
- 字段建议：
  - `error`
  - `retryable`（可选，第一版可不做）

说明：

- 第一版允许 provider 内部继续聚合 tool calls，只把文本 delta 先流出来。
- 上层最终仍以 `LLMResponse` 收口，降低 `TurnRunner` 重写成本。

### 2. `OpenAICodexProvider`：先落地流式实现（优先级最高）

当前条件很好：

- 已有 `_iter_sse()`（SSE event 解码）
- 已有 `_consume_sse()`（聚合为完整 `LLMResponse`）

建议做法：

1. 保留 `_consume_sse()` 不动（兼容 `chat()`）
2. 新增 `_stream_sse(...)` 或 `_iter_codemodel_events(...)`
   - 将 `response.output_text.delta` 转成 `text_delta`
   - 在适当时机聚合 tool calls
3. `stream_chat()` 使用 SSE 流，逐步 yield 流式事件

注意：

- 第一版不要求把每种 Codex 事件都暴露出去，只需满足 `text_delta + done` 最小闭环。
- `chat()` 仍然可复用旧逻辑，确保兼容性。

### 3. `TurnRunner`：新增流式消费分支（兼容现有 tool loop）

建议新增内部方法（示意）：

- `_chat_stream_once(...) -> tuple[LLMResponse, retry_stats]`

行为建议：

1. 检测 provider 是否支持 `stream_chat()`
2. 若支持：
   - 迭代读取事件
   - 对 `text_delta`：
     - 增量累积本地文本 buffer
     - 通过 `on_progress(...)` 发出增量（或节流后的聚合块）
   - 对 `done`：
     - 取得完整 `LLMResponse`
3. 若流式失败：
   - 第一版可以直接回退到现有 `_chat_with_retries()`（兼容优先）

关键点：

- 不直接改掉 `_chat_with_retries()`（先并行存在）
- 工具调用循环逻辑尽量复用原 `response.has_tool_calls` 分支

### 4. Progress 链路复用（先不改渠道协议）

第一版建议直接复用现有 progress 消息机制：

- `on_progress(delta)` -> `ProgressPublisher` -> bus -> channel

建议补一个最小节流（避免刷屏过多）：

- 时间窗合并（如 `100~200ms`）
- 或长度阈值合并（如累计到 `N` 字符再发）

节流位置建议：

- 优先放在 `TurnRunner` 内部（最小改动）
- 或在 `ProgressPublisher` 层统一处理（复用性更好）

### 5. S-Stream1 测试建议

至少覆盖：

1. provider 流式解析测试（Codex）
- 输入 SSE delta 序列
- 断言可产出 `text_delta` 与最终 `done`

2. `TurnRunner` 流式消费测试
- fake provider 返回 `text_delta` + `done`
- 断言 `on_progress` 被多次调用
- 断言最终 `final_content` 与非流式一致

3. 回退测试
- `stream_chat()` 抛错时回退 `chat()`
- 行为不崩溃

4. 非流式 provider 回归测试
- 未实现 `stream_chat()` 的 provider 继续走旧路径

---

## Phase S-Stream2（事件模型增强，已落地）

### S-Stream2 目标（状态：已落地，最小版）

- 把“流式文本输出”纳入内部事件模型（不再只靠 `on_progress`）
- 保持现有 turn/tool 事件兼容

### 建议新增事件（最小）

1. `message_delta`（或分层命名 `message.delta` 对应 `kind`）
- 字段建议：
  - `delta`
  - `content_len`（累计长度）
  - `role`（通常 `assistant`）

2. `message_start` / `message_end`（可选）
- 若要和平台文档风格对齐，可逐步补齐
- 第一版不强制

### 与现有事件模型的关系

- 保持 `turn_start/tool_start/tool_end/turn_end` 不变
- 新增事件纳入 `turn_event_capabilities()` manifest
- debug sinks 先记录，不急着做复杂消费

### 观测建议

- `message_turn_event_summary` 可增加：
  - `message_delta_count`
  - `streamed_text_chars`

---

## Phase S-Stream3（渠道体验优化，部分落地）

### S-Stream3 目标（状态：部分落地，最小版）

- 在不改变核心流式语义的前提下，提升渠道端体验
- 按渠道能力差异做分层实现

### 路径建议

1. 默认路径（兼容）
- 继续发送 progress 多条消息
- 所有渠道可用，成本最低
- 已补充每 turn 最大 progress 消息数限制，避免刷屏

2. 增强路径（按渠道）
- 支持消息编辑/替换的渠道：
  - 用“单条消息不断更新”实现更像 ChatGPT 的体验
- 不支持编辑的渠道：
  - 保持 progress 多条 + 最终完整答案
- 当前已落地可选 stream done marker（默认关闭），用于在不改消息编辑能力前提供可感知的流式收尾

3. 节流与合并策略
- 渠道层可配置：
  - `progress_flush_interval_ms`
  - `progress_min_chars`
  - `progress_max_messages_per_turn`

---

## Phase S-Stream4（工具流式更新与统一重试）

### S-Stream4 目标（更长期）

- 工具执行流式更新（`tool_execution_update`）
- 流式与非流式路径统一重试/失败处理策略
- 为未来 SSE/WebSocket API 输出打基础

### 候选能力

1. 工具流式更新
- `exec` 长输出实时增量
- web/下载类工具进度（如已知 total）

2. 统一 `FailoverPolicy` 接入流式路径
- 将 `Phase 3-2` 现有重试逻辑抽象成策略对象
- 流式失败时的回退/重试规则统一化

3. 对外流式接口（可选）
- 若未来要开放 API，可在 channel/bus 之外提供 SSE/WebSocket 输出

---

## 风险与缓解（现实版）

### 风险 1：流式改动影响现有稳定路径

缓解：

- 坚持双路径（`chat()` + `stream_chat()`）
- 非流式 provider 完全不变
- 流式失败可回退非流式

### 风险 2：progress 刷屏过多

缓解：

- 第一版就引入节流/合并
- 先用配置控制频率，不急着做复杂策略

### 风险 3：工具调用与文本流混合时序复杂

缓解：

- 第一版 provider 内部聚合 tool calls，先把“文本流式 + 最终工具调用”打通
- 不急着做工具参数 delta

### 风险 4：不同 provider streaming 能力差异大

缓解：

- 通过 `stream_chat()` 能力检测与 fallback 机制处理
- 先支持一条 provider（Codex），跑通后再扩展

---

## 与现有路线图的关系

### 与 `architecture-roadmap.md` 的关系

- 这条线是“事件模型 + 运行时体验”的延展，和 `Phase 4` 的扩展事件方向高度相关。
- 可作为 `Filter Chain` / `StateStorage` 之外的独立专题推进，不与 memory 路线冲突。

### 与当前已实现能力的关系

- 复用：
  - `TurnRunner`
  - `ProgressPublisher`
  - turn events（`namespace/version/kind`）
  - `/health?debug=events` 能力暴露
- 承接：
  - `Phase 3-2` 的重试/分类逻辑（后续可抽成 `FailoverPolicy`）

---

## 当前建议（执行策略）

1. 若准备马上实施，先只做 `S-Stream1`
- 一条 provider（`OpenAICodexProvider`）
- `TurnRunner` 流式消费分支
- progress 节流
- 回退到非流式路径

2. 实施时坚持“小步 + 可回退”
- 每一步都保留现有非流式兼容路径
- 先测 provider + `TurnRunner`，再调渠道体验

3. 先观察真实体验数据，再决定是否进入 `S-Stream2/S-Stream3`
- 看 progress 刷屏是否可接受
- 看实际流式收益是否明显（TTFT 体感改善）
