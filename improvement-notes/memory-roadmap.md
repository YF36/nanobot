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
- 已实施（step2+，2026-02-27）：daily 写入去噪增强：
  - fallback（`history_entry`）写入 daily 时仅写正文，不再带时间戳前缀；
  - 同一 daily section 内完全相同 bullet 自动去重（结构化写入与 fallback 均生效）。
- 已实施（step2+，2026-02-27）：fallback 质量收敛（保守规则）：
  - 去除常见模板化角色前缀（如 `User asked...`）；
  - 去除明显冗余元信息尾句（如 `This interaction indicates...` / `No new information added...`）；
  - 保持“仅规则压缩、不做语义改写”。
- 保持兼容：`history_entry` / `memory_update` 仍为必填主路径，旧模型输出不受影响。

当前策略决策（阶段性，2026-02-26）：

- 暂时保持 `M2-full step2` 兼容版现状（结构化 `daily_sections` + 启发式 fallback）。
- 暂不强化 consolidation prompt（仍保持“可选提供 `daily_sections`”）。
- 先收集一段时间真实数据，重点观察：
  - `structured_daily_ok` 命中率
  - `fallback_reason` 分布（`missing` / `empty` / `invalid_type:*` / `invalid_item:*`）
- 后续再决定是否升级为“优先提供 `daily_sections`”或进一步收紧 schema/提示词。

观测增强进展（2026-02-27）：

- 已实施：daily 路由结果落盘到 `memory/daily-routing-metrics.jsonl`（每次 consolidation 一行 JSON）。
- 字段覆盖：`structured_daily_ok`、`fallback_used`、`fallback_reason`、`structured_keys`、`structured_bullet_count`、`session_key`、`date`、`ts`。
- 用途：支持对 `structured_daily_ok` 命中率和 `fallback_reason` 分布做离线统计，不改变主流程行为。
- 已实施：`nanobot memory-audit --metrics-summary` 汇总输出（含总体命中率、fallback reason 分布、按天统计）。
- 已实施：`nanobot memory-audit --metrics-out <path>` 可导出指标汇总 Markdown。
- 已实施：fallback reason 纠偏建议映射（`metrics-summary` 中自动给出 top reason 对应修复建议）。
- 已实施：`memory_update` guard 触发指标落盘：`memory/memory-update-guard-metrics.jsonl`。
- 已实施：`nanobot memory-audit --guard-metrics-summary` 汇总 guard reason 分布与高频会话。
- 已实施：`nanobot memory-audit --guard-metrics-out <path>` 可导出 guard 指标汇总 Markdown。
- 已实施：`memory` 偏好冲突指标落盘：`memory/memory-conflict-metrics.jsonl`（当前覆盖 language / communication_style）。
- 已实施：`nanobot memory-audit --conflict-metrics-summary` 汇总冲突 key 分布与高频会话。
- 已实施：`nanobot memory-audit --conflict-metrics-out <path>` 可导出冲突指标汇总 Markdown。
- 已实施：`nanobot memory-observe` 一键生成“审计 + routing 指标 + guard 指标 + conflict 指标”日快照（默认输出到 `improvement-notes/memory-observations/`）。
- 已实施：`memory-observe` 额外输出 `observability-dashboard` 聚合报告（单文件汇总关键指标与下一步建议）。
- 已实施：`nanobot memory-audit --archive-dry-run --archive-keep-days N` 归档试点（只输出候选文件与体量，不改文件）。
- 已实施：`nanobot memory-audit --archive-out <path>` 可导出归档 dry-run 报告。

观测记录归档（2026-02-27）：

- 原始观测文件统一归档到 `improvement-notes/memory-observations/`：
  - `improvement-notes/memory-observations/20260227-audit.md`
  - `improvement-notes/memory-observations/20260227-cleanup-plan.json`
  - `improvement-notes/memory-observations/20260227-metrics-summary.md`
  - `improvement-notes/memory-observations/20260227-audit-v2.md`
  - `improvement-notes/memory-observations/20260227-metrics-summary-v2.md`
  - `improvement-notes/memory-observations/20260227-cleanup-effect-v1.md`
  - `improvement-notes/memory-observations/20260227-audit-v3.md`
  - `improvement-notes/memory-observations/20260227-metrics-summary-v3.md`
  - `improvement-notes/memory-observations/20260227-guard-metrics-summary-v3.md`
  - `improvement-notes/memory-observations/20260227-conflict-metrics-summary-v3.md`
- 路线图仅保留“可执行结论”，原始报表/计划留在归档目录，避免主文档噪声累积。
- 该批次结论摘要：
  - 审计显示记忆质量仍有漂移（`HISTORY` 长条目、`daily` 长 bullet/重复项仍存在）；
  - 清理计划建议优先做“裁剪超长 + 去重”（低风险）；
  - 指标汇总文件当前显示 `daily-routing-metrics.jsonl` 尚未生成，需要后续 consolidation 周期再采样。

受控清理闭环实测（2026-02-27）：

- 执行参数：`--apply --apply-recent-days 7 --apply-skip-history`（只清理最近 daily，跳过 HISTORY）。
- 实测结果（见 `20260227-cleanup-effect-v1.md`）：
  - `daily_trimmed_bullets=4`
  - `daily_deduplicated_bullets=5`
  - `DAILY long bullets`: `4 -> 0`
  - `DAILY duplicates`: `5 -> 0`
- 结论：第 3 项“受控清理闭环”在真实数据上有效，且作用范围可控（仅触达近 7 天 daily）。
- v3 快照现状：routing 指标继续可用；guard/conflict 指标文件尚未生成（说明尚未触发对应拒写/冲突事件）。
- 受控触发验证：已用临时样本目录验证 `--guard-metrics-summary` 与 `--conflict-metrics-summary` CLI 汇总路径可用，输出字段完整。

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

M3 前置保护（2026-02-27 已落地）：

- 已实施 `memory_update` 异常变更阈值保护（guard）：
  - 候选更新相对当前内容“过度收缩”时拒写；
  - `##` 标题保留率过低时拒写（结构突变保护）；
  - 拒写时记录告警日志并保留现有 `MEMORY.md`，防止一次异常 consolidation 覆盖长期记忆。

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

M3 最小实现进展（2026-02-27）：

- 已实施“按需读取 recent daily memory（最小版）”：
  - 默认不注入 daily 内容；
  - 当当前消息命中回顾类关键词（如“之前/上次/回顾/recall/previous”等）时，
    才动态注入最近 7 天的精简 daily bullet 片段（有条数与字符上限）。
- 已实施“按需读取二次收敛”：
  - 默认仅注入 `Topics / Decisions / Open Questions`；
  - `Tool Activity` 默认排除，仅当用户问题明确在问“工具/命令/操作记录”时再注入。
- 已实施“来源锚点增强”：
  - recent daily 注入携带 `date + section`（如 `YYYY-MM-DD [Topics]`），提高可追溯性。
- 目标：在不常驻增加 prompt 噪声的前提下，增强“回顾最近对话”场景的命中率。

M3 验收标准：

- 默认 prompt token 开销无明显上升。
- 近期问题回顾场景下可准确检索到 daily file 摘要。
- 归档后关键信息不丢失（抽样验证）。
- 若引入 TTL：过期清理可回放、可审计（备份/归档留痕）。

### M4 候选（吸收 `memory3.md` 的可执行原则）

基于 `memory3.md`，对 nanobot 当前实现最值得借鉴、且可低风险落地的点如下（按建议优先级）：

1. 前缀稳定性指标化（P1）

- 目标：把“前缀一致性”从原则变成可观测指标。
- 建议落地：
  - 在每次 provider 调用前记录 `prefix_hash` / `prompt_tokens` / `history_tokens`；
  - 计算并输出 `prefix_stability_ratio`（连续调用前缀不变比例）。
- 价值：帮助判断是否存在“无意动态 system prompt”或序列化抖动导致的 cache miss。
  - 进展（2026-02-27）：已落地最小实现（`memory/context-trace.jsonl` + `memory-audit --context-trace-summary`）。

2. 轻量 Context Trace（P9）

- 目标：对“上下文在每轮如何演化”形成可回放证据。
- 建议落地（JSONL）：
  - `stage`（before_compact / after_compact / before_send）
  - `message_count`
  - `estimated_tokens`
  - `prefix_hash`
  - `timestamp`
- 价值：为后续 pruning 阈值调优提供客观依据，而不是体感调参。
  - 进展（2026-02-27）：`memory-observe` 已纳入 context trace 快照导出。

3. 渐进降级的阶段分布观测（P4）

- 目标：验证“多数情况停留在低损耗阶段”的策略是否成立。
- 建议落地：
  - 统计 soft trim / hard clear / summary prune 的触发次数与占比；
  - 在 `memory-observe` 快照中增加 `pruning_stage_distribution` 摘要。
- 价值：避免过早进入高损耗压缩，保护关键记忆。
- 进展（2026-02-27）：
  - 已落地最小版阶段观测：`memory-audit --apply` 追加写入 `memory/cleanup-stage-metrics.jsonl`；
  - 已新增汇总输出：`memory-audit --cleanup-stage-summary`；
  - 已纳入 `memory-observe` 快照：新增 `*-cleanup-stage-summary.md`，并在 dashboard 增加 `Pruning Stage Distribution` 摘要。

4. 追加优先 + 转换不丢失（P2/P8）

- 目标：继续坚持 append-only 语义，同时保证可回溯。
- 建议落地：
  - 对被清理/归档内容保留结构化索引（日期、section、来源文件）；
  - 归档策略先 dry-run，再小流量 apply，始终保留回滚备份。
- 价值：控制噪声的同时避免“静默丢记忆”。
- 进展（2026-02-27）：
  - 已落地最小版“转换索引”：`memory-audit --apply` 在执行 trim/dedupe/drop 时，追加写入 `memory/cleanup-conversion-index.jsonl`（保留来源文件、section、action、标准化前后信息）。
  - 已新增汇总输出：`memory-audit --cleanup-conversion-summary`。
  - 汇总已增强：可显示“最近一次 cleanup run”的 `run_id` 与动作分布，便于快速判断最新一轮清理影响。
  - 已纳入 `memory-observe` 快照：新增 `*-cleanup-conversion-summary.md`，dashboard 增加 `Cleanup Conversion Traceability` 摘要（含 latest cleanup run）。
  - 数据质量提示：若 conversion index 有效行缺少 `run_id`，dashboard 会给出修复提示，避免误读历史清理影响。

5. 信息半衰期驱动的保留策略（C6）

- 目标：把“信息类型差异”显式化到保留规则中。
- 建议落地：
  - `Decisions` 默认保留更久；
  - `Tool Activity` 更短窗口，且默认不注入 recall；
  - 临时错误状态仅在观测文件保留，不进入长期记忆。
- 价值：减少“留垃圾、丢决策”的风险。
- 进展（2026-02-27）：
  - 已有：`--drop-tool-activity-older-than-days N`（按窗口清理过旧 `Tool Activity`）。
  - 新增：`--drop-non-decision-older-than-days N`（按窗口清理过旧 `Topics/Open Questions`，默认保留 `Decisions`）。
  - 说明：两项均为 `memory-audit --apply` 下的显式开关，默认关闭，便于灰度。
  - 新增观测阈值提示：`memory-observe` dashboard 会在 `drop_non_decision` 占比过高时给出窗口收敛建议，降低过清理风险。
  - 新增 apply 前预估：`memory-audit --apply-drop-preview [--apply-recent-days N]`，先输出候选删除体量（tool/non-decision）再决定是否执行 `--apply`。
  - 新增守卫开关：`memory-audit --apply --apply-abort-on-high-risk`，当预估风险为 `high` 时中止执行，避免误全量清理。
  - `memory-observe` 已纳入 30d 半衰期预估摘要（dashboard + `*-cleanup-drop-preview-summary.md`），用于日常先观测再执行。
  - `memory-observe` 的 drop preview 快照顶部增加风险统计行（risk level + total candidates），便于快速扫读。
  - drop preview 新增 `dominant driver`（`tool_activity/non_decision/mixed/none`），快速定位主要清理来源。
  - drop preview 新增 `Top candidate files`（默认前 3），便于快速定位受影响文件。
  - dashboard 的 `Half-Life Drop Preview (30d)` 区块已同步展示 `top candidate files`，减少跨文件查看。
  - 预估结果新增风险分级（`low/medium/high`），dashboard 在高风险时提示先缩小范围（如 recent-days 灰度）再全量 apply。
  - dashboard 的“下一步建议命令”已按风险级别动态分流（low/medium/high），减少人工判断与参数拼接。
  - 当 30d 预估无候选时，dashboard 明确输出 “No half-life cleanup candidates...” 提示，避免误判为数据缺失。
  - 预估报告新增“Recommended Next Command”，按风险级别给出建议命令，减少人工拼参数。
  - 当直接执行 `memory-audit --apply` 且存在半衰期候选删除时，CLI 会先打印自动预估摘要（risk/tool/non-decision/scope），高风险时给出灰度提示。

6. 约束优先级裁决（冲突处理模板）

- 目标：当策略冲突时有统一裁决标准，减少临时拍板。
- 建议采用：
  - `prefix稳定性 > append-only > 可中断性/防循环 > 渐进降级 > 按需加载 > 其他`
- 价值：后续设计评审可复用，不随实现者变化。

### M3 辅助工具现状（2026-02-27）

- 已有只读体检命令：`nanobot memory-audit`
  - 输出记忆质量报告与 dry-run 清理计划（JSON）。
- 已有保守清理开关：`nanobot memory-audit --apply`
  - 行为：仅做“裁剪超长 + 同文件去重”，并自动创建时间戳备份目录；
  - 定位：用于低风险收口，不涉及 TTL/抽象层/检索层策略变更。
- 已有受控清理范围开关：`nanobot memory-audit --apply --apply-recent-days N [--apply-skip-history]`
  - 行为：可限制只处理最近 N 天 daily files；可选择跳过 `HISTORY.md` 清理；
  - 适合灰度验证，避免一次性全量改写。
- 已有半衰期清理开关：`--drop-tool-activity-older-than-days N`
  - 行为：在 `--apply` 时可按天数清理过旧 `Tool Activity` bullet（仅删除该 section，不影响其他 section）。
- 已有清理闭环报告：`nanobot memory-audit --apply --apply-effect-out <path>`
  - 输出清理前后对比（`before/after/delta`），便于验证收益并做回滚决策。
