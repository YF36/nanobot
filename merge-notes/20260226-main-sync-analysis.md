# nanobot main 分支更新合并分析（对 feature/infrawavesbot）

## 分析范围

- 本次已将本地 `main` 从 `b2a1d12` 更新到 `a77add9`（`behind 84` -> 最新）。
- 重点分析区间：`b2a1d12..a77add9`
- 目标：评估 upstream 更新里哪些值得合并到当前改造分支 `feature/infrawavesbot`。

## 总体判断（结论先行）

值得优先吸收的 upstream 更新主要有 4 类：

1. `memory` 健壮性修复（高优先级，低冲突）
2. `/stop` + subagent 取消能力（高价值，但与我们 `AgentLoop` 重构冲突高，建议手工移植）
3. `Session.get_history()` 游标/对齐修复（高价值，中低风险）
4. Feishu / shell / web 等渠道与工具的实用修复（按需 cherry-pick）

不建议直接 cherry-pick 的主要原因：

- 我们分支已深度重构 `AgentLoop / MessageProcessor / TurnRunner / SubagentManager / ContextBuilder / ToolRegistry / ExecTool / MemoryStore`，与 upstream 改动高度重叠。
- 多数关键更新应采用“按 commit 思路手工移植”而不是直接 `cherry-pick`。

## 已落地进度（本分支）

截至当前分支进度，以下建议项已完成落地（手工移植）：

- P1-1 `MemoryStore.consolidate()` 工具参数类型健壮性（含 JSON 字符串 `arguments` 解析）
- P1-2 `Session.get_history()` 使用 `last_consolidated` 游标并对齐到 user turn
- P1-3（部分）`/stop` + subagent 按 session 取消：
  - 已实现 `SubagentManager.cancel_by_session(session_key)`
  - 已实现 `SpawnTool` 透传 `session_key`
  - 已在 `SessionCommandHandler` 接入 `/stop`（停止当前会话后台 subagent）
  - 未实现 upstream 的 task-based dispatch 全量 `/stop`（与当前 `AgentLoop` 架构差异较大）
- P2-5 `ExecTool path_append` 配置支持（`ExecToolConfig` / tool factory / shell 执行 env PATH 扩展）
- P2-6 `ContextBuilder` 稳定 system prompt / runtime context 下沉（最小版）：
  - `channel/chat_id` 从 system prompt 移出
  - 以独立 `Untrusted Runtime Context` 用户消息注入
  - 纳入 token budget 计算
- P1-4（部分）Feishu `post` 富文本图片提取与下载：
  - 已移植 `_extract_post_content()`（文本 + `image_key` 提取）
  - 已在 `msg_type == "post"` 路径下载嵌入图片并转入 `media`
  - 已补纯函数测试（post 文本/图片键提取）
  - 未补完整渠道集成测试（当前环境/依赖成本较高）

代表性落地提交：

- `e5a476b` `fix merge memory args parsing and session history alignment`
- `7840125` `feat add session-scoped subagent stop command`
- `7b5a334` `feat support exec path_append configuration`
- `3e9f4a1` `refactor move session runtime metadata out of system prompt`

## 文件交集热区（冲突风险高）

upstream 变更文件与我们分支已改文件的交集很多，重点冲突热区：

- `nanobot/agent/context.py`
- `nanobot/agent/loop.py`
- `nanobot/agent/memory.py`
- `nanobot/agent/subagent.py`
- `nanobot/agent/tools/registry.py`
- `nanobot/agent/tools/shell.py`
- `nanobot/agent/tools/spawn.py`
- `nanobot/session/manager.py`
- `nanobot/cli/commands.py`
- `nanobot/heartbeat/service.py`
- `nanobot/channels/feishu.py`

因此建议：

- 先吸收“测试与行为意图”
- 再在我们当前架构下手工实现等价修复/功能

---

## 建议优先合并（P1）

### 1. Memory consolidation 工具参数类型健壮性（强烈建议）

相关 upstream 提交：

- `3eeac4e` Fix: handle non-string tool call arguments in memory consolidation
- `cd5a8ac` Merge PR #1061（同主题）

upstream 解决的问题：

- 某些 provider 会把 `tool_call.arguments` 返回成 JSON 字符串（而不是 dict）
- 或返回异常类型，导致 `MemoryStore.consolidate()` 出错

对我们分支的适用性：

- **非常适用**。
- 我们已经增强了 `MemoryStore`（M1/M2），并且 `memory` 路径现在更关键（`MEMORY` 清洗、daily files、structured daily sections）。
- upstream 的这类健壮性修复能直接降低 memory pipeline 失败率。

和我们当前实现的关系：

- 我们已覆盖“`history_entry` / `memory_update` value 非字符串时序列化”的情况。
- **但仍应确认是否已覆盖 `args` 本身为 JSON 字符串**（upstream 修的是这一层）。

建议动作：

- 手工移植 upstream 的 `args` 类型处理逻辑到我们当前 `nanobot/agent/memory.py`
- 同时参考 upstream 测试 `tests/test_memory_consolidation_types.py`
  - 可吸收其 case 到我们 `tests/test_memory_store_rules.py` 或新增专项测试

---

### 2. `Session.get_history()` 使用 `last_consolidated` 游标并对齐 user turn（建议尽快）

相关 upstream 提交：

- `2f573e5` fix(session): get_history uses last_consolidated cursor, aligns to user turn

upstream 解决的问题：

- 直接截取最近 N 条可能把 tool_result / assistant 工具消息切在开头，形成“孤儿消息块”
- 历史读取未充分利用 `last_consolidated` 游标

对我们分支的适用性：

- **高价值**。
- 我们已经在 session/history 上做了大量增强（结构化 `_tool_details`、save 优化、memory consolidation 演进），这条修复和我们的方向一致。

冲突风险：

- 中低。`nanobot/session/manager.py` 我们改过保存逻辑，但 `get_history()` 修复通常可手工合并。

建议动作：

- 优先手工移植 `get_history()` 的两个核心行为：
  1. 从 `last_consolidated` 之后取 unconsolidated 历史
  2. 切片后丢弃开头非 user 消息，保证对齐 user turn

---

### 3. `/stop` 命令 + subagent 按 session 取消（已部分落地，后续可增强）

相关 upstream 提交（一组）：

- `3c12efa` feat: extensible command system + task-based dispatch with /stop
- `2466b8b` feat: /stop cancels spawned subagents via session tracking
- `4768b9a` fix: parallel subagent cancellation + register task before lock
- `cdbede2` refactor: simplify /stop dispatch...
- 测试：`tests/test_task_cancel.py`

upstream 提供的价值：

- `AgentLoop.run()` 改为 task-based dispatch，支持 `/stop` 取消当前 session 的活跃任务
- `SubagentManager` 增加 `session_key -> task_ids` 跟踪与 `cancel_by_session()`
- 补齐了取消路径测试（这点很值得借鉴）

对我们分支的适用性：

- **功能价值很高**，尤其你在 IM 场景下已经关注 `/stop` / `steer` 体验。
- 但 upstream 实现与我们当前架构差异很大：
  - 我们已经拆成 `MessageProcessor` / `TurnRunner` / `SessionCommandHandler`
  - 我们已有 `follow-up` + `steer` 机制
  - 直接 cherry-pick 会和现有消息编排冲突

当前状态（本分支）：

- 已完成 `/stop` 的低冲突版本：可停止当前 session 的后台 subagent 任务
- 尚未实现 upstream 的 task-based dispatch / 当前主 turn 取消语义

后续建议动作：

- 不直接 cherry-pick `loop.py`
- 若要继续增强 `/stop`，建议与我们现有 `follow-up/steer` 机制统一设计：
  - 明确 `/stop` 是否仅取消后台任务，还是也应终止当前 turn
  - 优先复用现有 `SessionCommandHandler` / `MessageProcessor` / `TurnRunner` 接口扩展
- 吸收 upstream `tests/test_task_cancel.py` 的剩余场景（映射到我们架构）

说明：

- upstream 的 `/stop` 和我们的 `steer/follow-up` 不冲突，属于互补能力。

---

### 4. Feishu 富文本 post 图片提取与下载（已部分落地）

相关 upstream 提交：

- `4f80336` feat(feishu): support images in post (rich text) messages

upstream 价值：

- Feishu `post` 类型消息不只提文本，还提取 `img` 节点 `image_key`
- 自动下载图片并进入 media 处理流程

对我们分支的适用性：

- **如果你主要通过 Feishu 使用 nanobot，这条很值得吸收**。
- 你近期反馈和验证主要发生在 Feishu 渠道，因此这是用户可感知更新。

冲突风险：

- 中等（我们分支也改过渠道层），但可局部手工移植。

当前状态（本分支）：

- 已手工移植 `_extract_post_content()` 与 `msg_type == "post"` 图片下载分支
- 已增加纯函数测试覆盖 `post` 文本 + `image_key` 提取
- 尚未补渠道端到端测试（依赖 Feishu SDK 与消息事件模拟）

后续建议动作：

- 如你近期 Feishu 使用频繁，建议补一条消息处理级测试（mock `_download_and_save_media` + `_handle_message`）
- 吸收 upstream 若后续还有 `post`/富文本兼容修复时，优先对照 `_extract_post_content()` 差异

---

## 建议合并（P2）

### 5. `ExecTool` `path_append` 配置支持（实用性增强）

相关 upstream 提交：

- `abcce1e` feat(exec): add path_append config to extend PATH for subprocess
- `07ae825` fix: pass path_append from config to ExecTool
- `7be2785` fix(exec): use empty default and os.pathsep for cross-platform

upstream 价值：

- 允许通过配置扩展子进程 PATH（例如本地工具链路径）
- 对跨平台路径分隔符做了修复

对我们分支的适用性：

- **中高价值**。我们已经重构了 `ExecTool`（guard/executor/formatter），这类配置能力适合补上。
- 但由于我们 `shell.py` 结构已大改，应手工移植到 `ShellExecutor`/`ExecTool` 当前结构中。

建议动作：

- 吸收行为，不 cherry-pick `shell.py`
- 同步检查 `config/schema.py`、`AgentLoop`、`SubagentManager` 的 `ExecTool` 构造路径

---

### 6. `ContextBuilder` 稳定系统 prompt + runtime context 下沉（已部分落地）

相关 upstream 提交（同一主题链）：

- `56b9b33` fix: stabilize system prompt for better cache reuse
- `87a2084` feat: add untrusted runtime context layer for stable prompt prefix
- `f294e9d`, `d55a850`（runtime context 注入重构）
- 测试：`tests/test_context_prompt_cache.py`

upstream 价值：

- 把时间、channel/chat_id 等 runtime metadata 从 system prompt 中移出，改成单独 user message
- 提升 system prompt 稳定性，利于 prompt cache 命中
- 明确 runtime context 是“metadata only, not instructions”（untrusted）

对我们分支的适用性：

- **理念高度契合**，尤其我们已经在 `ContextBuilder` 里做了 runtime tool catalog 与 metadata 剥离。
- 但冲突会很高，因为我们对 `context.py` 改动很大（tool catalog、`_tool_details`、structured result metadata）。

当前状态（本分支）：

- 已完成最小版落地：
  - 将 `channel/chat_id` 从 system prompt 中下沉到独立 `Untrusted Runtime Context` 消息
  - 增加测试验证不同 session 下 system message 保持一致
- 与我们现有 `Runtime Tool Catalog` 共存（仍在 system prompt 的 dynamic 部分）

后续建议动作：

- 继续吸收 upstream `tests/test_context_prompt_cache.py` 的其余测试意图（按需映射）
- 如后续继续优化 prompt cache 命中率，再评估是否进一步稳定 `Current Time` 粒度或拆分 dynamic sections

---

### 7. `memory` / `session` 回归测试新增（建议吸收测试思路）

相关 upstream 测试：

- `tests/test_memory_consolidation_types.py`
- `tests/test_task_cancel.py`
- `tests/test_context_prompt_cache.py`

对我们分支的适用性：

- 非常适合吸收“测试场景”，即使实现细节不同。
- 我们分支已经做了大量 memory/loop 重构，更需要用 upstream 的回归用例补盲区。

建议动作：

- 不必原样拷贝全部测试
- 选取场景映射到我们现有测试文件：
  - `tests/test_memory_store_rules.py`
  - `tests/test_agent_loop_run.py` / 新增 `test_stop` 相关文件
  - `tests/test_context_builder.py`

---

## 建议观察/按需合并（P3）

### 8. Heartbeat 服务重构（两阶段决策/执行）

相关 upstream 提交：

- `ec55f77`, `bfdae1b`, `7671239`, `8d1eec1`, `f828a1d`
- 新增测试：`tests/test_heartbeat_service.py`

upstream 方向：

- heartbeat 改成两阶段（决策 tool call + 执行）
- 弱化字符串 token（`HEARTBEAT_OK`）依赖
- 提升幂等性与消息投递行为

对我们分支的适用性：

- 功能价值存在，但不属于当前你最关心的主线（memory/M2、IM 体验、/new、steer）。
- 且 `heartbeat/service.py`、`cli/commands.py` 在我们分支也有较多改动。

建议：

- 暂不优先
- 后续若要提升 heartbeat 稳定性，再集中吸收这组提交思路和测试

---

### 9. 频道与工具零散修复（按需挑选）

包括（示例）：

- `ef57225`, `abd35b1` Web API key 每次调用时解析（配置热更新友好）
- `f8dc6fa` MCP HTTP transport timeout 行为修复
- `91e13d9` Email proactive send 与 autoReplyEnabled 解耦
- `1f7a81e`, `8686f06`, `81b669b`, `96e1730` Slack 渠道修复
- `4303026` Discord typing loop 错误恢复

建议：

- 根据你实际使用渠道/工具挑选
- 对于我们当前高频场景（Feishu、web、exec），优先级更高

---

## 我们分支已覆盖或部分覆盖的 upstream 思路（避免重复做）

以下 upstream 更新与我们现有改造方向有重叠，吸收时应“取差异，不重复”：

- base64 图片导致 session/history 膨胀（upstream `6aed426`, `a1440cf`）
  - 我们分支已做 `TurnHistoryWriter` 和内容裁剪/元数据剥离，需对照确认是否完全覆盖 user-content 中 `data:image/...` 场景。
  - 建议：补测试场景比直接移植更稳。

- `spawn` / `subagent` session 上下文绑定
  - upstream 给 `SpawnTool` 增加 `session_key` 并配合 `SubagentManager.cancel_by_session`
  - 我们分支已大改 subagent（复用 `TurnRunner`、事件 sink），应手工融合。

- 工具返回结构化信息 / 可观测性
  - 我们分支在 `ToolExecutionResult.details` 与审计日志方面已走得更远
  - upstream `registry.py` 的 docstring/轻量重构无需优先合并

---

## 推荐合并策略（实操顺序）

### 第 1 批（低风险高收益，建议先做）

1. `MemoryStore.consolidate()` 参数健壮性（`args` 为 JSON 字符串/异常类型）
2. `Session.get_history()` 使用 `last_consolidated` + user-turn 对齐
3. 吸收 `test_memory_consolidation_types.py` 核心用例

### 第 2 批（功能增强，但需手工移植）

1. `/stop` 命令 + subagent `cancel_by_session`（参考 `tests/test_task_cancel.py`）
2. Feishu `post` 图片提取与下载支持
3. `ExecTool path_append` 配置支持（映射到我们当前 shell 架构）

### 第 3 批（按需/后续）

1. Context prompt cache / untrusted runtime context（先吸收测试意图）
2. Heartbeat 两阶段重构
3. 其他渠道/工具零散修复（按使用频率挑选）

---

## 建议的下一步（如果继续这条 merge 线）

我建议先从这两个开始（收益最高且和当前主线兼容）：

1. 手工移植 `MemoryStore.consolidate()` 的 tool args 健壮性修复（含测试）
2. 手工移植 `Session.get_history()` 的 unconsolidated + user-turn 对齐修复（含测试）

这两项完成后，再评估 `/stop` 功能移植（会牵涉我们当前 `MessageProcessor/SessionCommandHandler` 架构）。
