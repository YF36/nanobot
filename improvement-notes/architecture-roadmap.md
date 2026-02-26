# nanobot 改进分析（对比 pi-mono / coding-agent）

## 范围与结论

本次对比主要看了：

- `pi-mono/packages/agent`（核心 agent loop / Agent 状态机）
- `pi-mono/packages/coding-agent/src/core/agent-session.ts`（会话编排、扩展、压缩、重试）
- `pi-mono/packages/coding-agent/src/core/tools/*`（重点 `read` / `edit` / `bash`）
- `nanobot/nanobot/agent/*`、`nanobot/nanobot/agent/tools/*`、`nanobot/nanobot/session/manager.py`

结论（先说重点）：

1. `nanobot` 当前实现已经有不少实用能力（多渠道总线、MCP、子代理、记忆归档、安全守卫），但核心循环与工具层边界偏“粘”，后续扩展成本会快速升高。
2. 最值得借鉴 `pi-mono` 的，不是 UI/TUI，而是它把“Agent内核 / 会话编排 / 工具实现”三层拆开的方式。
3. `nanobot` 的第一优先级重构应放在：
   - 抽离 `AgentLoop`（拆职责）
   - 升级工具执行协议（结构化结果 + 可流式更新）
   - 消除 `SubagentManager` 中重复的 mini-agent loop

## 两个框架的核心差异（架构层）

### nanobot（当前）

- `AgentLoop` 同时负责消息消费、命令处理、上下文构建、LLM调用、工具执行、会话保存、内存归档调度、进度回传，职责集中在一个类中（`nanobot/nanobot/agent/loop.py:38`, `nanobot/nanobot/agent/loop.py:424`）。
- 工具执行协议是“字符串输入/字符串输出”为主，`ToolRegistry.execute()` 会把异常和参数校验统一转成字符串错误（`nanobot/nanobot/agent/tools/registry.py:58`）。
- `SubagentManager` 内部复制了一套独立 agent loop（`nanobot/nanobot/agent/subagent.py:111` 起）。

### pi-mono agent（核心内核）

- `agent-loop.ts` 专注循环与事件流，返回 `EventStream`，用事件表达 turn/message/tool 生命周期（`pi-mono/packages/agent/src/agent-loop.ts:28`, `pi-mono/packages/agent/src/agent-loop.ts:94`）。
- `Agent` 只管状态、队列（steer/follow-up）、订阅、调用 loop（`pi-mono/packages/agent/src/agent.ts:96`, `pi-mono/packages/agent/src/agent.ts:405`）。
- 通过 `convertToLlm` + `transformContext` 给上层保留消息转换/裁剪扩展口（`pi-mono/packages/agent/src/types.ts:48`, `pi-mono/packages/agent/src/types.ts:67`）。

### pi-mono coding-agent（会话/产品层）

- `AgentSession` 在 `Agent` 之上做会话、工具启停、扩展、自动压缩、自动重试、命令/技能扩展等（`pi-mono/packages/coding-agent/src/core/agent-session.ts:213`）。
- `AgentSession` 本身很强大，但也已经出现“大类”倾向（单文件 ~2865 行）；这点反而是 nanobot 可以“借鉴其架构方向、避免其体量”的地方。

## nanobot 可以优先借鉴的点（来自 pi-mono）

## 1. 事件化的 Agent 内核（高优先级）

`pi-mono` 的核心价值是事件模型清晰：

- `agent_start / turn_start / message_* / tool_execution_* / turn_end / agent_end`（`pi-mono/packages/agent/src/types.ts:179`）
- 工具执行可发 `tool_execution_update`，UI/日志/持久化/扩展层都能复用（`pi-mono/packages/agent/src/agent-loop.ts:324`）

`nanobot` 目前只有字符串型 `on_progress`，表达能力不足（`nanobot/nanobot/agent/loop.py:256`, `nanobot/nanobot/agent/loop.py:552`）。

建议：

- 在 `nanobot` 增加内部事件模型（哪怕先不对外暴露）。
- 先做最小集合：
  - `turn_start`
  - `assistant_delta`（可选）
  - `tool_start`
  - `tool_end`
  - `turn_end`
- 用事件替代 `on_progress(str)`，再由 CLI/渠道层把事件渲染为文本进度。

收益：

- 统一进度、审计、持久化、UI 适配。
- 为后续“中断/排队/自动重试”做基础。

## 2. 中断与排队机制（steering / follow-up）（高优先级）

`pi-mono` 支持：

- 运行中插入“打断消息”（steer）
- 运行结束后排队“后续消息”（follow-up）
- 在工具执行后轮询用户打断，并跳过剩余工具调用（`pi-mono/packages/agent/src/agent-loop.ts:113`, `pi-mono/packages/agent/src/agent-loop.ts:363`, `pi-mono/packages/agent/src/agent.ts:248`）

`nanobot` 当前一次消息处理是“同步跑完整个 turn”，没有用户级打断队列（`nanobot/nanobot/agent/loop.py:256`）。

建议（分阶段）：

1. 先支持 session 级消息队列（只在 turn 边界注入）。
2. 再在工具循环里加“打断轮询点”（每个工具执行后检查）。
3. 最后再考虑跳过剩余工具调用并写入 `skipped` tool result。

这对聊天渠道体验提升很明显，尤其是 shell/web 工具耗时时。

## 3. 工具协议升级为“结构化结果 + 可选增量更新”（高优先级）

`pi-mono` 工具不是纯字符串返回，而是：

- `content`（text/image block）
- `details`（结构化详情，如 diff / truncation / fullOutputPath）
- `onUpdate` 增量推送（`pi-mono/packages/agent/src/types.ts:146`, `pi-mono/packages/agent/src/types.ts:153`, `pi-mono/packages/agent/src/agent-loop.ts:324`）

`nanobot` 当前 `Tool.execute()` 强制返回 `str`（`nanobot/nanobot/agent/tools/base.py:42`），导致：

- UI 只能做字符串解析
- 无法稳定展示 diff、截断元信息、文件路径、产物路径
- 无法流式展示工具输出

建议：

- 新增 `ToolResult` 数据结构（兼容旧工具）：
  - `text: str`
  - `details: dict[str, Any] | None`
  - `artifacts: list[...] | None`（可后续）
- `ToolRegistry.execute()` 先兼容：
  - 旧工具 `str` -> 自动包装为 `ToolResult(text=...)`
  - 新工具 -> 原样传递
- `ContextBuilder.add_tool_result()` 先继续把 `text` 写入上下文，`details` 先只用于日志/持久化。

## 4. 工具实现的“工厂化 + 后端可插拔 operations”（高优先级）

`pi-mono` coding-agent 的工具设计很适合未来扩展到远程执行（SSH / 容器 / 沙箱）：

- `createReadTool(cwd, options)` / `createEditTool(...)` / `createBashTool(...)`（`pi-mono/packages/coding-agent/src/core/tools/index.ts:107`）
- `ReadOperations` / `EditOperations` / `BashOperations` 把本地文件/进程操作抽象出来（`pi-mono/packages/coding-agent/src/core/tools/read.ts:27`, `pi-mono/packages/coding-agent/src/core/tools/edit.ts:35`, `pi-mono/packages/coding-agent/src/core/tools/bash.ts:35`）

`nanobot` 目前工具实例化散在 `AgentLoop._register_default_tools()` 和 `SubagentManager._run_subagent()` 两处（`nanobot/nanobot/agent/loop.py:113`, `nanobot/nanobot/agent/subagent.py:122`）。

建议：

- 新增 `nanobot.agent.tools.factory`（或 `runtime.py`）统一创建工具集合。
- 把文件系统与 shell 工具改为 `create_*_tool(config, ops=...)` 风格。
- 后续接入 MCP/远程 runner 时，不用复制工具逻辑。

## 5. 工具 UX 细节（read/edit/bash）很值得抄（高优先级）

### `read` 工具（pi-mono 的优点）

- 支持 `offset/limit` 分页读（`pi-mono/packages/coding-agent/src/core/tools/read.ts:11`）
- 文本截断时给出下一次读取建议（`... Use offset=... to continue`，`pi-mono/packages/coding-agent/src/core/tools/read.ts:165`）
- 图片读支持自动 resize 和 image block（`pi-mono/packages/coding-agent/src/core/tools/read.ts:102`）

### `edit` 工具（pi-mono 的优点）

- 保留换行风格/BOM（`pi-mono/packages/coding-agent/src/core/tools/edit.ts:121`）
- 返回 diff + 首行变更位置（`pi-mono/packages/coding-agent/src/core/tools/edit.ts:24`, `pi-mono/packages/coding-agent/src/core/tools/edit.ts:200`）

### `bash` 工具（pi-mono 的优点）

- 支持中止信号 + 进程树清理（`pi-mono/packages/coding-agent/src/core/tools/bash.ts:103`）
- 流式输出增量回调（`pi-mono/packages/coding-agent/src/core/tools/bash.ts:226`）
- 大输出滚动截断 + 落盘 temp file（`pi-mono/packages/coding-agent/src/core/tools/bash.ts:187`, `pi-mono/packages/coding-agent/src/core/tools/bash.ts:264`）

`nanobot` 当前：

- `read_file` 无分页（`nanobot/nanobot/agent/tools/filesystem.py:84`）
- `edit_file` 不返回 diff（`nanobot/nanobot/agent/tools/filesystem.py:222`）
- `exec` 一次性等待 `communicate()`，结果只做字符串截断（`nanobot/nanobot/agent/tools/shell.py:101`, `nanobot/nanobot/agent/tools/shell.py:136`）

建议直接对标这三点，性价比非常高。

## 6. `transformContext` / `convertToLlm` 这种“边界扩展口”（中高优先级）

`pi-mono` 在核心 loop 层就预留了：

- `transformContext(messages)`：做上下文裁剪/注入（`pi-mono/packages/agent/src/types.ts:67`, `pi-mono/packages/agent/src/agent-loop.ts:211`）
- `convertToLlm(messages)`：把内部消息类型映射到 LLM 协议（`pi-mono/packages/agent/src/types.ts:48`, `pi-mono/packages/agent/src/agent-loop.ts:217`）

`nanobot` 目前内部 message 结构与 provider 协议绑定更紧，`ContextBuilder` 直接产出 provider 风格 dict（`nanobot/nanobot/agent/context.py:394`）。

建议：

- 给 `AgentLoop` 增加可选 hook：
  - `pre_llm_messages_transform(messages) -> messages`
  - `provider_message_adapter(messages) -> messages`
- 先默认 no-op，后续用于：
  - 实验性消息压缩策略
  - provider 差异适配
  - 自定义会话元消息

## 7. 自动重试与上下文溢出恢复编排（中优先级）

`coding-agent` 的会话层做了：

- 可重试错误识别（`pi-mono/packages/coding-agent/src/core/agent-session.ts:2083`）
- 自动重试
- 自动 compaction，并在 overflow 后自动 continue（`pi-mono/packages/coding-agent/src/core/agent-session.ts:1565`, `pi-mono/packages/coding-agent/src/core/agent-session.ts:1615`, `pi-mono/packages/coding-agent/src/core/agent-session.ts:1721`）

`nanobot` 已有 memory consolidation，但触发与恢复策略主要依赖会话长度阈值，和 LLM overflow/error 的编排耦合较少（`nanobot/nanobot/agent/loop.py:522`, `nanobot/nanobot/agent/memory.py:193`）。

建议：

- 先加“provider 错误分类”（overflow / retryable / fatal）。
- 对 overflow 场景支持：
  - 记录错误
  - 触发 compact
  - 自动重试当前 turn 一次

## 8. 工具集合与系统提示的联动（中优先级）

`coding-agent` 能按 active tool names 重建 system prompt，并把工具开关状态反映到提示词（`pi-mono/packages/coding-agent/src/core/agent-session.ts:607`, `pi-mono/packages/coding-agent/src/core/agent-session.ts:674`, `pi-mono/packages/coding-agent/src/core/agent-session.ts:1961`）。

`nanobot` 工具注册是固定的（`nanobot/nanobot/agent/loop.py:113`），系统提示虽有工具使用指导，但不随工具集变化（`nanobot/nanobot/agent/context.py:147`）。

建议：

- 支持按 channel / mode 配置工具集（如只读、禁 web、禁 exec）。
- `ContextBuilder` 接收当前工具列表，生成“当前可用工具说明”。

## nanobot 当前代码中值得重构的点（重点）

## A. `AgentLoop` 过重（首要重构）

症状：

- 单文件承担太多职责：总线消费、会话命令、上下文预算、LLM循环、工具执行、进度推送、记忆归档调度、session 持久化（`nanobot/nanobot/agent/loop.py:38`, `nanobot/nanobot/agent/loop.py:327`, `nanobot/nanobot/agent/loop.py:424`）。
- `_process_message()` 很长且包含 `/new`、`/help` 等命令分支（`nanobot/nanobot/agent/loop.py:467`）。

风险：

- 新功能（中断、重试、扩展、更多命令）都会继续堆进这个类。
- 单元测试会越来越依赖大量 mock。

建议拆分（最小可行）：

1. `TurnRunner`
   - 负责 `_run_agent_loop()` 与 tool-call 执行
2. `MessageProcessor`
   - 负责 `_process_message()` 主流程
3. `SessionCommandHandler`
   - 处理 `/new`、`/help` 等命令
4. `ConsolidationCoordinator`
   - 负责 `_consolidating` / lock / task 生命周期
5. `ToolContextBinder`
   - 负责 `message/spawn/cron` tool 上下文注入

先拆类，不改行为。

## B. `SubagentManager` 重复实现 agent loop（高优先级）

当前 `SubagentManager._run_subagent()` 复制了 tool-call 循环、assistant/tool message 拼接逻辑（`nanobot/nanobot/agent/subagent.py:153` 起）。

问题：

- 主 agent 与 subagent 的行为逐渐漂移（例如未来主 agent 加重试/中断/上下文预算后，subagent 不会自动获得）。
- 工具注册也重复（`nanobot/nanobot/agent/subagent.py:122`）。

建议：

- 让 subagent 复用 `TurnRunner`（或复用一个轻量 `AgentRuntime`）。
- 通过参数注入限制：
  - 工具集（无 `message`、无 `spawn`）
  - `max_iterations`
  - system prompt

## C. 工具基类与 `ToolRegistry` 过于“字符串化”（高优先级）

### 当前问题

- `Tool.execute()` 只返回 `str`（`nanobot/nanobot/agent/tools/base.py:42`）
- `ToolRegistry.execute()` 把错误、校验、提示语拼在字符串里（`nanobot/nanobot/agent/tools/registry.py:58`）
- `_HINT` 文案硬编码在执行层（`nanobot/nanobot/agent/tools/registry.py:69`）

这会导致：

- 执行层与提示词策略/交互文案耦合
- 无法稳定表达 `diff/truncation/file_path`
- 后续 UI/审计要靠字符串解析

建议：

- 引入 `ToolExecutionResult` / `ToolExecutionError`
- `ToolRegistry` 只返回结构化结果，不拼“请换个方法重试”的提示
- 让“错误提示增强”在 prompt strategy 层做

## D. `filesystem.py` 存在较多重复代码（中高优先级）

优点先说：安全性处理很好，尤其是 symlink 链检查和 `O_NOFOLLOW`（`nanobot/nanobot/agent/tools/filesystem.py:15`, `nanobot/nanobot/agent/tools/filesystem.py:34`）。

可重构点：

- 四个工具重复 `__init__` 参数、路径解析、审计日志、错误包装（`nanobot/nanobot/agent/tools/filesystem.py:84`, `nanobot/nanobot/agent/tools/filesystem.py:133`, `nanobot/nanobot/agent/tools/filesystem.py:185`, `nanobot/nanobot/agent/tools/filesystem.py:273`）。
- `read_file` 没有分页参数，长文件很容易污染上下文（`nanobot/nanobot/agent/tools/filesystem.py:100`）。
- `edit_file` 成功信息过于简略，未返回 diff（`nanobot/nanobot/agent/tools/filesystem.py:238`）。

建议：

- 抽 `BaseFilesystemTool`（封装 `_resolve_path`、审计、错误模板）
- `read_file` 增加 `offset` / `limit`
- `edit_file` 返回 unified diff（可复用现有 `difflib`）

## E. `ExecTool` 把“安全策略、执行、输出格式化”混在一起（中高优先级）

当前 `ExecTool` 同时负责：

- 安全规则（regex + shlex + 路径检查）（`nanobot/nanobot/agent/tools/shell.py:19`, `nanobot/nanobot/agent/tools/shell.py:154`, `nanobot/nanobot/agent/tools/shell.py:188`）
- 子进程执行（`nanobot/nanobot/agent/tools/shell.py:101`）
- 结果拼装与截断（`nanobot/nanobot/agent/tools/shell.py:121`）

建议拆分：

- `ShellGuard`（策略与审计）
- `ShellExecutor`（spawn / timeout / cancel）
- `ShellOutputFormatter`（截断与显示）

并逐步补齐：

- 中断信号支持（当前只有 timeout，没有外部 cancel）
- 流式输出回调
- 大输出落盘 + 尾部截断（参考 pi-mono `bash`）

## F. `ContextBuilder` 职责过多（中优先级）

`ContextBuilder` 同时做：

- system prompt 组装（bootstrap/skills/memory）（`nanobot/nanobot/agent/context.py:77`）
- history compaction + budgeting（`nanobot/nanobot/agent/context.py:232`, `nanobot/nanobot/agent/context.py:329`）
- 图片压缩与 multimodal message 构造（`nanobot/nanobot/agent/context.py:468`, `nanobot/nanobot/agent/context.py:497`）
- message append helper（`nanobot/nanobot/agent/context.py:517`, `nanobot/nanobot/agent/context.py:544`）

建议拆分：

- `SystemPromptBuilder`
- `HistoryCompactor`
- `MultimodalContentBuilder`
- `ConversationMessageBuilder`（append assistant/tool）

这样也能减少 `AgentLoop` 对 `ContextBuilder` 私有方法的调用（目前直接调用 `_compact_history` / `_trim_history`，`nanobot/nanobot/agent/loop.py:235`）。

## H. `SessionManager.save()` 每次全量重写 JSONL（中优先级）

`SessionManager.save()` 每次都会重写整个文件（`nanobot/nanobot/session/manager.py:155`）。

在对话变长后会带来：

- IO 放大
- 崩溃恢复窗口变大（写到一半）

建议（渐进式）：

1. 保持现格式不变，先实现“增量 append message + 定期写 metadata”。
2. 再考虑引入 entry-based 日志（参考 `coding-agent` 的 session entry 思路，`AgentSession` 中可见 appendMessage/appendCompaction 等调用，`pi-mono/packages/coding-agent/src/core/agent-session.ts:343`, `pi-mono/packages/coding-agent/src/core/agent-session.ts:1509`）。

## I. 命令处理应从 `AgentLoop` 中抽离（中优先级）

`/new` / `/help` 直接写在 `_process_message` 内（`nanobot/nanobot/agent/loop.py:467`），后续命令一多会继续膨胀。

建议：

- 命令注册表（类似 `dict[str, handler]`）
- `CommandContext`（session、bus、consolidator、tool binder）

这也方便做 channel-specific 命令。

## J. `/new` 命令的记忆压缩应考虑“响应优先、后台收尾”（中优先级，已落地最小版）

现象（用户体验问题）：

- 在 IM 渠道（如 Feishu）里发送 `/new` 后，如果旧 session 触发记忆压缩（consolidation），用户会明显等待很久才收到 `New session started.`。

判断：

- `/new` 的核心语义是“切换到新 session”，不是“等待旧 session 压缩完成”。
- 因此适合把“旧 session 的 consolidation”从 `/new` 同步路径中移出，改为后台收尾（最终一致）。

已落地（最小版，`81b0e57`）：

1. `/new` 先取消同 session 的 in-flight consolidation（保留原保护）
2. 立即清空/切换 session 并回复 `New session started.`
3. 将旧 session 的 snapshot archival（`archive_all=True`）交给 `ConsolidationCoordinator.start_background(...)`
4. 后台归档失败只记录日志，不阻塞 `/new` 响应

观测增强（已落地）：

- `/new background archival scheduled`（含 `reason="session_reset"`、`snapshot_len`）
- `/new background archival done`（含 `elapsed_ms`、`success=true`）
- `/new background archival failed/errored`（含 `elapsed_ms`）
- `/new background archival summary`（每 10 次汇总一次：`total/ok/failed/errored/avg_elapsed_ms`）

说明（语义变化）：

- `/new` 不再同步等待归档成功，因此“归档失败不清 session”的旧语义已调整为“响应优先 + 最终一致后台收尾”。

原建议方案（设计思路，已实现最小版）：

1. `/new` 同步完成：
   - 创建/切换新 session
   - 立即回复 `New session started.`
2. 对旧 session：
   - 如需压缩，交给 `ConsolidationCoordinator.start_background(...)`
   - 增加来源标记（如 `reason="session_reset"` / `source="command:/new"`）便于日志观测
3. 后台任务失败：
   - 仅记录 warning/debug，不影响新 session 可用性

后续可增强（未实施）：

- 旧 session 与后续消息可能并发，需继续依赖 `ConsolidationCoordinator` 的 session 级排他/任务管理。
- 不建议把 `/new` 的全部逻辑异步化；仅异步化“慢收尾”（consolidation + 可选二次保存）。
- 可选增强：先同步快速保存旧 session，再后台做压缩（两阶段收尾）。
- 建议先观察一段时间 `/new background archival summary` 的成功率与平均耗时，再决定是否推进“两阶段收尾”。

## 我建议的落地顺序（ROI 排序）

## Phase 1（高 ROI，低风险，先做）

1. 工具协议升级（兼容旧 `str` 返回）
2. `filesystem` 增加 `read_file(offset, limit)`、`edit_file` diff 返回
3. `ExecTool` 拆 guard/executor/formatter（行为不变）
4. 抽 `ToolFactory`，消除主 agent / subagent 工具注册重复

状态（截至 2026-02-24）：已完成

- 已完成：工具协议兼容升级（`ToolExecutionResult` + `ToolRegistry.execute_result()`）
- 已完成：`filesystem` 工具 UX 增强（分页读、`edit_file` diff）
- 已完成：`ExecTool` 内部拆分为 guard/executor/formatter（行为保持）
- 已完成：主 agent / subagent 工具工厂去重

## Phase 2（中风险，收益大）

1. `AgentLoop` 拆分：`MessageProcessor` + `TurnRunner` + `ConsolidationCoordinator`
2. 引入内部事件模型（先不改外部接口）
3. `SubagentManager` 复用 `TurnRunner`

状态（截至 2026-02-24）：已完成（按最小版目标）

- 已完成：`AgentLoop` 拆分（`TurnRunner`、`SessionCommandHandler`、`ConsolidationCoordinator`、`MessageProcessor`、`TurnHistoryWriter` 等）
- 已完成：`SubagentManager` 复用 `TurnRunner`
- 已完成：内部事件模型（最小版，内部使用；外部接口仍保持 `on_progress` 兼容）

## 已落地进展（截至 2026-02-24）

下面是本次分析后已在代码中完成并提交的主要改造（按主题归类）：

### 核心运行时与架构拆分

- 已落地：`AgentLoop` 主循环拆分为 `TurnRunner` / `MessageProcessor` / `SessionCommandHandler` / `ConsolidationCoordinator`
- 已落地：`TurnHistoryWriter` 抽离（会话 turn 持久化、内容裁剪、图片占位）
- 已落地：`MessageProcessor` 再拆为 handlers/helpers/types 模块（`message_processor.py` / `message_processor_helpers.py` / `message_processor_types.py`）
- 已落地：`SubagentManager` 复用 `TurnRunner`，消除重复 mini-agent loop
- 已落地：`SubagentManager` prompt/result announcement helper 拆分（可读性提升）
- 已落地：内部事件模型（最小版）接入 `TurnRunner` / `AgentLoop` / `MessageProcessor`（内部 debug/可观测性）
- 已落地：内部事件 payload 基础 trace 字段（`turn_id` / `sequence` / `timestamp_ms` / `source`）
- 已落地（部分）：内部事件扩展协议最小版
  - 事件 payload 增加 `namespace` / `version`（协议扩展字段）
  - 保留 `type` 兼容，同时增加分层 `kind`（如 `turn.start` / `tool.end`）
  - `AgentLoop` / `Subagent` / `MessageProcessor` debug sink 开始消费 `kind`
  - 已落地：`/health?debug=events` 暴露 `turn_event_capabilities`（调试/适配器探测用；默认 `/health` 不变）
- 已落地（部分）：`steering/follow-up` 最小版
  - 同 session `follow-up` 排队串行处理
  - 工具执行后检测到 pending follow-up 时提前结束当前 turn（`steer v1`）
- 已落地（部分）：`Phase 3-2` 最小版（`TurnRunner` LLM 调用自动重试 + overflow compaction fallback）
  - transient 异常/`finish_reason=error` 的有限重试
  - 上下文超限错误的额外 compaction 重试
  - `turn_end` 事件携带 LLM 重试统计（retry 次数、overflow compaction 次数）
  - 已进一步细化 retry policy：仅对明确瞬时错误重试；认证/权限/请求类错误不重试（fatal）
  - `turn_end` 事件携带 `finish_reason=error` 分类命中统计（`overflow/retryable/fatal`）
- 已落地（部分）：`Phase 3-3` 最小版（动态工具集与 system prompt 联动）
  - `ContextBuilder.build_messages(...)` 支持基于运行时注册工具生成 `Runtime Tool Catalog`
  - `TurnMessageBuilder` 透传 `tools.get_definitions()`，避免 system prompt 与真实工具集脱节
  - 已深化：`Runtime Tool Catalog` 按能力分组（Filesystem / Shell / Messaging / Subagents / ...）
  - 已深化：同组工具按名称稳定排序，减少注册顺序导致的 prompt 抖动
  - 已深化：工具摘要包含 `required` 参数提示；高风险工具附加简短 `note`
  - 已深化：支持 compact mode（工具数量或 catalog 长度超阈值时降级输出）
  - 已深化：compact 阈值支持 `ContextBuilder` 初始化时配置（默认值不变）

### 工具运行时与兼容层

- 已落地：`ToolExecutionResult` 结构化工具结果类型（兼容旧 `str` 返回）
- 已落地：`ToolRegistry.execute_result()` 兼容层（保留旧 `execute()` 字符串接口）
- 已落地：`ToolRegistry` 审计日志增加结构化结果可观测字段：`has_details` / `detail_op` / `is_error`
- 已落地：`TurnRunner` 会话 `_tool_details` metadata envelope（含 `schema_version`）
- 已落地：`ContextBuilder` 在发给 LLM 前剥离 `_tool_details`（会话可存、模型不可见）
- 已落地：结构化 tool `details` 的 `op` 常量与基础 helper 收口（`nanobot/agent/tools/tool_details.py`）

### 工具能力与 UX（对标 pi-mono 的高 ROI 部分）

- 已落地：`read_file` 支持 `offset` / `limit` 分页读取
- 已落地：`edit_file` 返回 diff 预览与首个变更行提示
- 已落地：`exec`（shell）内部职责拆分（guard / executor / formatter）
- 已落地：`exec`（shell）结构化 `details`（超时/阻断/退出码/输出截断等）
- 已落地：`filesystem` 工具结构化 `details`
- 已落地：`message` 工具结构化 `details`
- 已落地：`spawn` 工具结构化 `details`

### Session 持久化（`SessionManager.save()`）

- 已落地：跳过未变化 session 的重复保存（减少全量 JSONL 重写）
- 已落地：恢复 session metadata 中的 `updated_at` 读取（避免保存去重误判）
- 已落地：session 保存 observability（`session_save_written` / `session_save_skipped`，含耗时与计数）
- 已落地：session 文件原子写（临时文件 + `replace()` + `fsync`）
- 已落地：周期性 session save summary metrics（跳过率、区间平均耗时等）
- 已落地：`list_sessions()` 首行 metadata 读取缓存（按 `mtime_ns + size` 失效）
- 已落地：`list_sessions()` 坏文件读取失败 debug 日志（不再静默吞错）

### 测试与验证基线

- 已落地：补充 `filesystem` 工具结构化结果测试
- 已落地：补充 `turn_runner` `_tool_details` envelope 测试
- 已落地：补充 `TurnRunner` 内部事件流测试（顺序、`turn_id`、`sequence`、`timestamp_ms`、`source`）
- 已落地：补充 `message` / `spawn` 工具结构化结果测试
- 已落地：补充 `ToolRegistry` 审计 `detail_op` 在 `edit_file` / `exec` / `message` / `spawn` 的真实工具覆盖
- 已落地：补充 `SessionManager.save()` 去重/原子写/周期汇总观测测试
- 已落地：补充 `list_sessions()` metadata 缓存命中/失效与坏文件 debug 日志测试
- 已落地：补充 `TurnRunner` LLM 自动重试与 overflow compaction fallback 测试
- 已验证：Phase 2 回归基线（选定 pytest 子集）`219 passed`

### 代表性提交（节选）

- `ae59c25` `refactor tools runtime and improve filesystem tool UX`
- `df27c47` `refactor agent loop turn and consolidation orchestration`
- `8941ec8` `refactor agent message processing orchestration`
- `d315e14` `refactor subagent loop to reuse turn runner`
- `06d4849` `feat structured edit_file tool details`
- `2b9ee6f` `feat retain structured tool details in session history`
- `fd8e551` `feat structured exec tool details`
- `2f8bf25` `feat structured details for message and spawn tools`
- `b40d463` `feat add internal turn event hooks in agent loop`
- `98d0e0a` `feat add message-level turn event stats`
- `40a6215` `feat add turn ids to internal turn events`
- `14e1ab9` `feat add event sequence numbers to turn events`
- `099ad03` `feat add timestamps to internal turn events`
- `6dedc40` `feat add source labels to turn events`
- `75462e5` `feat include turn ids in message event summaries`
- `6159e38` `perf dedupe unchanged session saves`
- `ab43316` `feat add session save observability logging`
- `40e47a3` `feat write session files atomically`
- `30c4083` `feat add periodic session save summary metrics`
- `528e84c` `perf cache session list metadata reads`
- `fa0fa27` `refactor unify structured tool details constants`
- `090ef93` `feat queue follow-up messages per session`
- `d0c3069` `feat interrupt turns for pending follow-up messages`
- `f93ad15` `feat retry llm chat with overflow compaction fallback`
- `e57dde4` `feat emit llm retry metrics in turn events`
- `c561bdc` `refactor narrow llm retry policy to transient errors`
- `ab3276b` `feat track llm error classification metrics`
- `d131171` `feat add runtime tool catalog to system prompt`
- `90f74d0` `refactor group runtime tool catalog by capability`
- `2d6421b` `feat include required params in tool catalog`
- `5791cf6` `feat add caution notes to runtime tool catalog`
- `1826486` `feat add grouped tool catalog guidance hints`
- `4367dd3` `refactor stabilize tool catalog ordering`
- `f61ffce` `feat add compact runtime tool catalog mode`
- `dfb2fce` `feat compact tool catalog on count or length`
- `0068b66` `feat make tool catalog compact thresholds configurable`
- `fb8d2bd` `feat add event namespace and version fields`
- `2a9d0cf` `feat add hierarchical turn event kinds`
- `a5b32fe` `refactor prefer event kind in debug sinks`
- `5e8ac05` `feat expose turn event capabilities via health debug`

## Phase 3（能力升级）

1. steering/follow-up 队列
2. 自动重试 + overflow 自动 compact/重试
3. 动态工具集与 system prompt 联动

状态（截至 2026-02-25）：部分完成（3/3，含最小版）

- 已部分完成：`steering/follow-up`（follow-up 队列 + `steer v1` 工具后让出）
- 已部分完成：自动重试 + overflow 自动 compact/重试（`TurnRunner` 最小版 + 事件重试指标）
- 已部分完成：动态工具集与 system prompt 联动（最小版运行时工具目录注入）

## Phase 4（更长期）

1. 扩展点（插件/扩展事件）
2. 会话结构升级（entry-based / 分支）
3. 远程执行后端（SSH/容器）通过 tools operations 接口接入

## 企业级架构文档借鉴点（融合到 nanobot 路线图）

参考文档：`/Users/fanpucheng/proj/智能体平台技术架构设计.md`

这份文档对 nanobot 的价值不在于“照搬企业平台架构”，而在于提供了几个可缩小落地的抽象：`Filter Chain`、`StateStorage`、事件/观测契约、`FailoverPolicy`。下面按 nanobot 当前阶段的可落地程度分层。

### A. 适合近期吸收（高优先级）

1. `Filter Chain` 最小版（内部 pre/post filters）
- 借鉴点：`Filter -> FilterResult(action=continue/reject/short_circuit)` 的执行模型（文档 `4.4`）。
- 适配 nanobot 的最小版目标：
  - 先只做内部链路，不做插件化公开 API。
  - 先支持 `pre`（输入侧）和 `post`（输出侧）两类过滤器。
  - 用于承载未来的输入审计、RAG 注入、输出审计/脱敏等能力，减少逻辑继续堆在 `MessageProcessor` / `ContextBuilder` / `AgentLoop`。
- 与现有进展关系：
  - 已有事件模型、`MessageProcessor` 拆分、runtime metadata 下沉（`Untrusted Runtime Context`）后，切入点已经比较成熟。

2. `StateStorage` 接口化（文件实现适配器）
- 借鉴点：可插拔状态存储接口（文档 `4.7`, `5.5`）。
- 适配 nanobot 的最小版目标：
  - 先抽接口，不替换现有文件持久化。
  - 以当前 `SessionManager` 为默认文件实现（类似 `FileSessionStorage`）。
  - 为后续 Redis/PostgreSQL 留接口边界，而不是现在直接引入数据库。
- 与现有进展关系：
  - `SessionManager.save()` 已完成一轮优化（去重、原子写、summary metrics、`list_sessions` 缓存），现在做接口抽象的风险较低。

3. 事件/观测契约继续收口（OTEL 风格字段命名，先不强上 OTEL）
- 借鉴点：文档 `4.5` 的事件流模型 + `4.8` 的分层埋点思路。
- 适配 nanobot 的最小版目标：
  - 在现有 `turn_events` 基础上统一字段命名规范（如 latency/tokens/cost/error 分类）。
  - 保持 JSON 日志与内部事件模型对齐，先形成“类 OTEL”字段风格。
  - 暂不引入完整 OTEL SDK/Collector。
- 与现有进展关系：
  - 已有 `namespace/version/kind`、capabilities manifest、`/health?debug=events`，只差继续规范消费侧字段。

4. `FailoverPolicy` 结构化（承接 Phase 3-2）
- 借鉴点：模型失败重试/降级策略对象（文档 `4.6.3`）。
- 适配 nanobot 的最小版目标：
  - 将当前 `TurnRunner` 内的重试与 overflow compact fallback 规则逐步抽为策略对象。
  - 先不做多模型自动降级，只做“策略结构化 + 配置化边界”。
- 与现有进展关系：
  - Phase 3-2 已完成：重试、分类、指标、overflow fallback，已经具备抽策略的基础。

### B. 中期可做（按需推进）

1. 插件/Hook 生命周期（先内部后公开）
- 借鉴点：文档 `4.9` 的插件生命周期与 Hook 事件。
- 建议方式：
  - 先做内部 Hook/扩展点，稳定后再考虑对外插件 API。
  - 避免在内核边界尚未稳定时过早暴露公共接口。

2. 工具流式更新（`tool_execution_update`）
- 借鉴点：文档 `4.5.1` / `4.5.2` 的工具更新事件与 `onUpdate`。
- 建议方式：
  - 等确实需要流式 shell/web/tool 进度时再做。
  - 先让事件模型支持该类型，再逐步接入工具执行器。

### C. 暂缓（平台化过重，不适合当前 nanobot 主线）

1. LLM 意图路由（平台级语义路由）
- 文档 `4.3` 的价值主要在多 Agent 企业平台。
- 对当前 nanobot（个人助手/单实例）会增加额外延迟与配置复杂度，短期性价比不高。

2. 多租户、认证鉴权、预算/计费体系
- 文档 `6.*` 与 `4.6.4` 更偏平台化治理能力。
- 可作为未来平台化版本参考，不建议进入当前主线。

3. 完整 OTEL 基础设施（Collector/Jaeger/Tempo）
- 方向正确，但当前先把事件与日志契约收口，比直接上完整 OTEL 更务实。

### 建议落地顺序（结合当前路线图）

1. `Filter Chain` 最小版（内部）
2. `StateStorage` 接口化（文件实现适配）
3. 事件/观测字段规范化（承接现有 turn events）
4. `FailoverPolicy` 结构化（承接 Phase 3-2）

这样可以在不进入“企业平台化重构”的前提下，吸收这份架构文档里最有价值的抽象。

## 额外说明：nanobot 当前做得好的地方（建议保留）

- 文件系统工具的路径安全和 symlink 防护做得扎实（`nanobot/nanobot/agent/tools/filesystem.py:15`, `nanobot/nanobot/agent/tools/filesystem.py:34`, `nanobot/nanobot/agent/tools/filesystem.py:67`）。
- Shell 工具有多层安全 guard（regex + shlex + workspace 路径限制）（`nanobot/nanobot/agent/tools/shell.py:19`, `nanobot/nanobot/agent/tools/shell.py:154`, `nanobot/nanobot/agent/tools/shell.py:188`）。
- Memory consolidation 的 chunking / 长记忆截断策略比较务实（`nanobot/nanobot/agent/memory.py:123`, `nanobot/nanobot/agent/memory.py:149`, `nanobot/nanobot/agent/memory.py:193`）。
- `AgentLoop` 已有对上下文预算的防守性处理（`nanobot/nanobot/agent/loop.py:215`）。

## 一个最小改造蓝图（建议）

如果只做一轮“小步重构”，我建议目标是下面这个结构：

- `AgentLoop`（保留对外接口）
- `TurnRunner`（LLM 调用 + 工具循环 + 事件）
- `ToolRuntime`（registry + factory + structured result adapter）
- `SessionRuntime`（session save/load + command dispatch + consolidation scheduling）
- `ContextPipeline`
  - `SystemPromptBuilder`
  - `HistoryCompactor`
  - `MultimodalBuilder`

这样能在不重写产品能力的前提下，把 nanobot 的后续演进空间打开。
