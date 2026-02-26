# nanobot 记忆系统路线图（从 nanobot-improvements 拆分）

本文件聚焦记忆系统相关改进：`MemoryStore.consolidate()`、`MEMORY.md/HISTORY.md`、daily files，以及 M1/M2/M3 路线图。

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

