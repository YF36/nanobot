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

## G. `MemoryStore.consolidate()` 复杂度高（中优先级）

这个模块功能很强，但实现层次较深（嵌套循环 + 多种退出路径，`nanobot/nanobot/agent/memory.py:193` 起）。

建议在不改行为前提下做“提纯”：

- 抽出 `_call_consolidation_llm(...)`
- 抽出 `_apply_save_memory_tool_call(...)`
- 抽出 `_process_chunk(...) -> ChunkProcessResult`

目标不是“更短”，而是让边界更清楚，便于加：

- provider 错误分类
- overflow 恢复统计
- 并发/中断策略

## G2. 记忆系统可引入 Daily Files 作为短期→中期过渡层（中优先级，设计建议）

（补充参考：`ai-agent-memory` 的 `Q1/Q2/Q3`、TTL、`L0/L1/L2` 思路；本节已按 nanobot 当前阶段做取舍融合。）

结合 `openclaw` / `pi-mono` 一类实践，建议在 `memory/` 目录下引入按天的记忆文件（例如 `memory/2026-02-25.md`），作为 `MEMORY.md` 与 `HISTORY.md` 之间的过渡层。

问题诊断（当前 `MEMORY.md` 污染）：

- 现有 `MEMORY.md` 中容易混入大量近期对话内容、知识性问答原文摘要、临时系统状态（例如某几天讨论主题/百科型资料）。
- 这类内容与 `MEMORY.md` 的目标（长期稳定事实）不一致，会导致：
  - 常驻 prompt 噪声增大
  - 长期偏好/约束被淹没
  - token 浪费与上下文污染

根因判断：

- 问题不在“双文件架构（`MEMORY.md` + `HISTORY.md`）”本身，而在于 `MEMORY` 写入准入规则过宽。
- consolidation/记忆提炼阶段把“聊过的内容”误当成了“应长期记住的内容”。

改进目标：

- 保持 nanobot 的极简记忆设计，但收紧长期记忆准入，并引入一个“短期→中期缓冲层”。

建议分层（保持 nanobot 极简）：

- `memory/MEMORY.md`：长期事实（严格准入，低频更新）
- `memory/YYYY-MM-DD.md`：每日摘要（对话主题、关键决策、工具活动、重要事件）
- `memory/HISTORY.md`：长期可 grep 流水/归档（更粗粒度）

建议准入规则（实施时可写入 consolidation prompt / 规则）：

- 应进入 `MEMORY.md`：
  - 用户稳定偏好（语言、沟通方式、工具偏好）
  - 长期项目上下文/目标
  - 稳定环境与约束（常用平台、路径习惯、长期限制）
- 应进入 daily file（而非 `MEMORY.md`）：
  - 今日讨论主题
  - 知识问答摘要
  - 工具调用结果摘要、调试过程、阶段性结论
  - 临时系统异常/状态（可后续归档）
- 应丢弃或仅保留在短期上下文：
  - 大段百科内容、表格原文、可随时再查的通用知识正文

设计要点（实施时）：

1. Daily file 写摘要而非原始对话全文，避免重复 `HISTORY.md`。
2. `MEMORY` 提炼优先从 daily file 归纳，而不是直接从原始消息大量搬运。
3. Daily file 默认不常驻 system prompt，仅在需要回顾近期上下文时按需读取。
4. 可加保留/归档策略（如保留最近 N 天 daily files，之后压缩进 `HISTORY.md`）。
5. 给临时系统状态（如 API 错误）增加“易过期”处理策略，避免长期留在 `MEMORY.md`。

这样可以在不推翻 nanobot 现有双文件记忆思路的前提下，显著降低 `MEMORY.md` 噪声与 prompt 污染。

建议实施路线图（M1 / M2 / M3）：

### M1（优先，低风险）：先收紧 `MEMORY.md` 准入规则，不改存储结构

目标：先止住 `MEMORY.md` 污染（保持 `MEMORY.md + HISTORY.md` 结构不变）。

实施建议（先做规则与 prompt，不做结构升级）：

1. 在 consolidation / 记忆提炼 prompt 中明确禁止写入：
   - 今日讨论主题列表
   - 大段知识问答原文/表格
   - 工具调用长输出摘要
2. 要求写入 `MEMORY.md` 的内容必须同时满足：
   - 与用户/长期项目相关
   - 跨会话仍有价值（稳定性）
3. 对临时系统状态（API 故障、一次性异常）默认降级为：
   - 不写入 `MEMORY.md`，或写成更短、更可过期的条目

M1 验收标准（建议实现后用测试覆盖）：

- 应写入 `MEMORY.md`：用户长期偏好、长期目标、稳定环境约束。
- 不应写入 `MEMORY.md`：当天动漫/百科问答详情、剧情表格、长篇知识性摘要。
- 不应写入 `MEMORY.md`：一次性工具输出细节（长命令输出、网页长摘录）。
- `MEMORY.md` 的新增内容长度/条目数相较当前策略显著下降（可用简单统计验证）。

M1 状态（截至 2026-02-26）：已落地（含 M1.x 观测增强）

- 已实施：consolidation prompt 收紧 `MEMORY.md` 准入规则（仅长期稳定事实）。
- 已实施：后验清洗（移除明显“近期讨论主题” section；过滤系统问题 section 中的临时状态/一次性报错行）。
- 已实施：清洗分类日志与样例片段（用于观察误杀率）。
- 已新增测试：`tests/test_memory_store_rules.py`（覆盖 prompt 规则、后验清洗、分类统计、consolidate 写入路径）。
- 实测反馈：通过 `/new` 触发记忆压缩后，效果明显改善（长期记忆污染降低）。

建议下一步：先观察几天清洗日志命中情况，再决定是否推进 `M2`（daily memory files）。

### 融合方案（参考 ai-agent-memory 的可借鉴机制）

在保持 nanobot 当前 `M1 / M2 / M3` 主线不变的前提下，可吸收 `ai-agent-memory` 的三个关键思想，但按阶段引入，避免一次性扩大范围：

- **先融规则，不先加层数**：先把 `Q1 / Q2 / Q3` 判断框架用于约束写入去向（长期记忆 vs daily file vs 丢弃）。
- **再融分层写入**：先落地 daily files（`memory/YYYY-MM-DD.md`）作为短期→中期缓冲层。
- **最后再融自动治理/检索层**：TTL、`.abstract`、`insights/lessons` 等作为中后期能力。

可借鉴点（建议时机）：

- `Q1/Q2/Q3`（立即可用，适合并入 M1/M2 写入规则）
  - `Q1`: 不看会做错事 → 长期记忆（`MEMORY.md`）
  - `Q2`: 将来可能需要查 → daily file / 后续归档（先不进 `MEMORY.md`）
  - `Q3`: 两者都不是 → 仅短期上下文或丢弃
- `P0/P1/P2 + TTL`（稍后引入，建议在 M2 稳定后）
  - nanobot 当前 `MEMORY.md` 尚未条目化，不宜立即上自动 TTL 清理
  - 建议等 `MEMORY.md` 条目格式趋稳后再引入 janitor 类脚本
- `L0/L1/L2`（中长期目标，不建议现在直接做）
  - `L2`（daily files）可作为 M2 直接落地
  - `L0`（`.abstract`）与 `L1`（`insights/lessons`）建议放到 M3 之后再评估

这样可以保留 nanobot 的极简路线，同时避免把“记忆污染问题”过早升级为“复杂知识系统建设”问题。

### M2（中风险）：引入 daily memory files（`memory/YYYY-MM-DD.md`）

目标：为近期主题与事件提供稳定落点，避免继续挤入 `MEMORY.md`。

建议分两步实施（先小后大）：

#### M2-min（推荐先做，低风险）

- 仅新增 daily file 写入，不改变现有读取与 prompt 注入策略。
- consolidation 成功后在保留 `HISTORY.md` 写入行为不变的前提下，额外把 `history_entry` 追加到当天 `memory/YYYY-MM-DD.md`。
- daily file 先使用简单结构（如 `# YYYY-MM-DD` + `## Entries`），先验证“分层写入”是否稳定。

M2-min 状态（截至 2026-02-26）：已落地

- 已实施：`consolidate()` 在写入 `HISTORY.md` 的同时，追加写入 `memory/YYYY-MM-DD.md`。
- 已实施：daily file 首次创建写入简易模板（`# YYYY-MM-DD` + `## Entries`）。
- 保持不变：不读取 daily file、不注入 prompt（仅新增写入层）。
- 已新增测试：`append_daily_history_entry()` 生成/追加行为；`consolidate()` 路径同步写入 daily file。
- 回归验证：`tests/test_memory_store_rules.py` + `tests/test_consolidation_race.py` 通过。

#### M2-full（在 M2-min 稳定后）

- 按 `Q1 / Q2 / Q3` 明确输出去向：
  - `Q1`（长期稳定且跨会话有价值）→ `MEMORY.md`
  - `Q2`（将来可能要查）→ daily file（后续可归档）
  - `Q3`（短期/噪声）→ 不入长期存储（可仅留 session/history）
- daily file 升级为固定模板（Topics / Decisions / Tool Activity / Open Questions）。
- 仍然只写摘要，不写原始对话全文。

M2-full 状态（截至 2026-02-26）：部分落地（step2 兼容版）

- 已实施（step1）：daily file 从 `## Entries` 升级为固定模板（`Topics / Decisions / Tool Activity / Open Questions`）。
- 已实施（step1）：基于 `history_entry` 的轻量启发式分栏路由（兼容旧 `## Entries` 文件）。
- 已实施（step1）：daily 分栏路由 debug 日志（含 section 与短样例）用于观察命中质量。
- 已实施（step2，兼容版）：`save_memory` 工具 schema 支持可选 `daily_sections`（`topics/decisions/tool_activity/open_questions`）。
- 已实施（step2，兼容版）：`consolidate()` 优先写入结构化 daily sections；结构非法或缺失时回退到启发式分栏。
- 保持兼容：`history_entry` / `memory_update` 仍为必填主路径，旧模型输出不受影响。

当前策略决策（阶段性，2026-02-26）：

- 暂时保持 `M2-full step2` 兼容版现状（结构化 `daily_sections` + 启发式 fallback）。
- 暂不强化 consolidation prompt（仍保持“可选提供 `daily_sections`”）。
- 先收集一段时间真实数据，重点观察：
  - `structured_daily_ok` 命中率
  - `fallback_reason` 分布（`missing` / `empty` / `invalid_type:*` / `invalid_item:*`）
- 后续再决定是否升级为“优先提供 `daily_sections`”或进一步收紧 schema/提示词。

M2 验收标准：

- 当天会自动生成 daily file，且同日多次 consolidation 追加到同一文件。
- `HISTORY.md` 现有行为保持兼容（无回归）。
- `MEMORY.md` 与 daily file 的写入路由符合准入规则（可用抽样/测试验证）。
- daily file 不包含大段逐轮原始对话复制。

### M3（策略层）：按需读取 daily files + 保留/归档策略（并为 TTL / L0/L1/L2 做准备）

目标：让 daily files 提升近期回忆能力，但不增加常驻 prompt 噪声，并为后续 TTL、`.abstract`、`insights/lessons` 留好接口。

M3 设计原则（补充，吸收 Clawdbot/相关文章思路）：

- 按目的分层优先于纯时间分层：
  - 不只区分“长期/短期”，还要区分用途（如用户偏好、任务状态、项目知识、工具活动、工作日志）。
  - 同样是“近期内容”，不同用途的写入/检索/遗忘策略应不同。
- 事实层与认知层分离：
  - 事实层：客观事件与证据（如 `HISTORY.md`、daily file 中的 `Tool Activity` / 原始摘要）。
  - 认知层：经过准入机制筛选后的长期记忆（`MEMORY.md`）。
  - 后续 M3/M4 设计应避免把“事实流水”再次写回 `MEMORY.md`。
- 可审计性优先：
  - 记忆条目应逐步具备来源可追踪能力（来自哪次会话/哪段摘要、何时写入）。
  - 即使暂不做条目级 ID，也应在日志与 daily file 写入中保留足够证据线索。
- 生命周期与可逆性：
  - 临时状态、过期偏好、旧决策需要可降权/过期/替换。
  - 未来引入 TTL / janitor 时，目标不只是“省空间”，而是避免矛盾记忆与脏记忆长期驻留。

实施建议：

- daily file 默认不注入 system prompt，只在需要回顾近期上下文时按需读取。
- 设置保留窗口（如最近 7~30 天），更旧 daily files 归档/压缩进 `HISTORY.md`。
- 为临时系统状态增加过期/降权策略。
- （可选，后段）引入 `P0/P1/P2` 标记与 TTL janitor：
  - 前提：`MEMORY.md` 条目格式已稳定、误删风险可控
- （可选，中长期）引入 `L0/L1/L2` 分层读取：
  - `L2`: daily files（本阶段已有）
  - `L1`: insights / lessons（LLM 反思提炼或结构化教训）
  - `L0`: `.abstract` 目录摘要（优先读，降低 token）

M3 验收标准：

- 默认 prompt token 开销无明显上升。
- 近期问题回顾场景下可准确检索到 daily file 摘要。
- 归档后关键信息不丢失（抽样验证）。
- 若引入 TTL：过期清理可回放、可审计（备份/归档留痕）。

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
