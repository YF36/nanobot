# nanobot 改进路线图（索引）

本目录用于管理 nanobot 的改进思路、实施进度与阶段性决策。

## 文档索引

- `architecture-roadmap.md`：AgentLoop/工具层/上下文/事件模型/命令与系统级改进路线图。
- `memory-roadmap.md`：记忆系统（M1/M2/M3）路线图、设计原则与阶段性决策。
- `sandbox-roadmap.md`：沙箱抽象层（S1-S4）设计路线图，面向 `exec` 工具的多后端隔离执行方案。
- `streaming-roadmap.md`：真正流式输出（token/delta）改造路线图，兼容现有 `chat()` 路径渐进接入。
- `../merge-notes/*.md`：按日期记录的 upstream 合并分析与落地进度。

## 当前建议使用方式

- 讨论架构/执行/工具/渠道能力时：优先看 `architecture-roadmap.md`。
- 讨论记忆系统（MEMORY/HISTORY/daily files）时：优先看 `memory-roadmap.md`。
- 讨论本地工具执行隔离（Docker/gVisor/boxlite 抽象层）时：优先看 `sandbox-roadmap.md`。
- 讨论模型/渠道流式输出改造时：优先看 `streaming-roadmap.md`。
- 讨论与 upstream `main` 的差异和吸收计划时：查看 `merge-notes/`。
