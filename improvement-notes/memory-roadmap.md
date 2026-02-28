# nanobot Memory Roadmap (Rewrite)

更新时间：2026-02-28（v2 — 融合代码审查改进建议）
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
- 全文替换时 LLM 产出质量波动（某次遗漏某 section），guard `heading_retention_too_low` 能拦截但拦截后本次 memory_update 完全丢失，无法做增量修复。

问题：难做精细更新、冲突解决和可逆回滚。

### 3.4 生命周期链路可观测但可逆性不完整

- 已有 archive/compact/ttl 审计日志。
- 但缺少“按 run_id/metrics 恢复”的标准回滚能力。

问题：自动化清理策略不易扩大灰度范围。

### 3.5 指标很多，但缺统一 SLO 门槛

- 已有 routing/guard/sanitize/cleanup 大量指标。
- 但缺明确 release gate（例如何时从 compatible 升 preferred）。

问题：决策依赖人工体感，自动化不足。

### 3.6 写入操作缺乏原子性保障

- `write_long_term()` 直接调用 `Path.write_text()`，异常断电或进程 crash 时可能导致 MEMORY.md 写入不完整（partial write）。
- `append_history()` 和 daily file 写入同样缺少 atomic write 保护。

问题：与 `data safety` 最高优先级矛盾，存在数据损坏风险。

### 3.7 Consolidation 中断恢复缺失

- `consolidate()` chunk 循环中，如果进程在 chunk 之间中断，`session.last_consolidated` 已更新，但后续 chunk 的数据未处理，等于静默丢失了这些消息的 consolidation。

问题：策略优先级明确 `interruption safety` 高于 `recall quality`，但当前实现未兑现。

### 3.8 Guard/Sanitize 指标缺少“正常通过”基线

- 当前指标只在触发时记录（guard hit / sanitize removal），缺少“正常通过”的计数。
- 无法计算真实的 guard 触发率，Phase Gate 的自动判定缺少分母。

问题：决策依赖人工体感，与 3.5 的 SLO 需求直接关联。

### 3.9 Daily Recall 关键词匹配精度不足

- `_should_include_recent_daily_memory` 使用硬编码关键词匹配（“之前”、“recall” 等）。
- False negative：用户说“上周讨论的方案”不会触发（缺少“上周”关键词）。
- False positive：“previously” 作为通用词可能误触发。

问题：recall 质量依赖触发精度，但当前实现偏粗放。

### 3.10 memory_maintenance.py 重复模式过多

- 2,289 行中大量代码是“JSONL 读取 → 解析 → 聚合 → summary dataclass → 渲染 markdown”的重复模式。
- 每新增指标类型需写一套几乎相同的 summarize + render 函数。

问题：维护成本随指标类型数量线性增长。

### 3.11 Preference Conflict 检测覆盖面窄

- `_detect_preference_conflicts` 仅检测 `language` 和 `communication_style` 两个 key。
- 硬编码正则 + section 名匹配，无法扩展到更多偏好维度。
- 检测到冲突后只记录日志，缺少 resolution 策略。

问题：无法有效保护用户偏好一致性。

## 4. 重构目标（2026Q1）

1. **数据安全**：写入操作原子化，消除 partial write 风险。
2. **模块化**：把 memory 主链拆成可独立测试的组件，引入 Pipeline 抽象。
3. **策略化**：把 routing/guard/lifecycle 变成显式 policy。
4. **增量更新**：MEMORY.md 从全文替换升级为 section-level merge，消除 LLM 遗漏导致的数据丢失。
5. **中断安全**：consolidation 支持中断恢复，不丢失未处理 chunk。
6. **可逆性**：所有 destructive 操作支持 run-level 恢复。
7. **可运营**：建立可执行的 SLO 与 rollout 门槛，补全指标基线。
8. **兼容迁移**：不破坏现有 workspace 数据结构。

## 5. 目标架构（Target Shape）

### 5.1 逻辑分层

- `memory_domain.py`
  - 结构定义：HistoryEntry、DailySections、MemoryUpdateCandidate、PolicyDecision、SectionDict
- `memory_io.py`
  - 原子写入（write-tmp-then-rename）、文件读取、JSONL append
- `memory_consolidation.py`
  - chunk/scope/retry/tool-call orchestration
  - `ConsolidationPipeline`：显式步骤编排（见 5.3）
- `memory_section_merge.py`
  - MEMORY.md 的 section-level 解析与 merge（见 5.4）
- `memory_routing_policy.py`
  - `daily_sections_mode`、fallback 决策、优先级裁决
- `memory_guard_policy.py`
  - sanitize + guard + conflict rules
  - preference conflict resolution 策略（keep_old / keep_new / ask_user / merge）
- `memory_lifecycle.py`
  - cleanup/archive/compact/ttl/restore
  - consolidation 中断恢复（progress journal）
- `memory_observability.py`
  - 指标 schema、append、summary 辅助
  - `JsonlMetricsSummarizer`：通用 JSONL 聚合框架
  - `memory-update-outcome.jsonl`：每次 memory_update 写入结果（含正常通过基线）

`memory.py` 最终仅作为 facade（薄入口）。

### 5.2 策略优先级（统一裁决）

统一采用：

`data safety > append-only traceability > interruption safety > recall quality > token cost`

说明：

- 任何策略优化不得突破 data safety（例如禁止静默覆盖 MEMORY）。
- recall 优化与 token 预算冲突时，优先保安全与可追溯。

### 5.3 Pipeline 抽象（Consolidation）

引入 `ConsolidationPipeline` 模式，替代当前 `_apply_save_memory_tool_call` 中的线性 if-else：

```python
@dataclass
class PipelineContext:
    session_key: str
    raw_args: dict
    current_memory: str
    memory_truncated: bool
    # 各阶段产出
    entry: str | None = None
    routing_plan: DailyRoutingPlan | None = None
    sanitized_update: str | None = None
    guard_decision: str | None = None

class ConsolidationPipeline:
    steps = [
        NormalizeHistoryStep,
        RouteDailySectionsStep,
        SanitizeMemoryUpdateStep,
        GuardMemoryUpdateStep,
        SectionMergeStep,
        WriteStep,
    ]
```

好处：每个 step 可独立单测、独立灰度开关、独立指标采集。

### 5.4 Section-level Merge（MEMORY.md）

MEMORY.md 从全文替换升级为 section-level merge，消除 LLM 遗漏某 section 导致全量丢失的风险：

```python
# 将 MEMORY.md 解析为 Dict[heading, List[bullet]]
def parse_sections(md: str) -> SectionDict: ...

def section_merge(current: SectionDict, candidate: SectionDict) -> SectionDict:
    result = dict(current)  # 保留所有现有 section
    for heading, bullets in candidate.items():
        if heading in result:
            result[heading] = dedupe_merge(result[heading], bullets)
        else:
            result[heading] = bullets
    return result

def render_sections(sections: SectionDict) -> str: ...  # 序列化回 markdown
```

好处：
- `heading_retention_too_low` guard 可从“拦截”变为“自动修复”
- 不破坏 markdown 兼容（最终仍序列化为 markdown）
- 为 R5 的结构化迁移铺路

## 6. 分阶段实施计划

### Phase R0：基线冻结 + 数据安全加固（短周期）

目标：确保后续重构有可比较基线，同时修复数据安全隐患。

**基线冻结：**

- 固化样本集：
  - 正常对话写入
  - 工具高频会话
  - 缺失/错误 `daily_sections`
  - `/new` 触发 consolidation 场景
- 固化指标快照模板（routing + guard + sanitize + cleanup + ttl）
- **[新增] Golden output 自动化回归**：
  - 创建 `tests/fixtures/memory_golden/` 目录，包含 3-5 个典型场景的输入（messages list + current MEMORY.md）和期望输出
  - 用 `pytest.mark.golden` 标记，每个 golden case 验证：
    - `_normalize_history_entry` 输出
    - `_resolve_daily_routing_plan` 的 source 字段
    - `_sanitize_memory_update_detailed` 的 removed counts
    - `_memory_update_guard_reason` 的 reason
  - 后续 R1 重构时这些 golden test 即为回归保障

**[新增] 数据安全加固（P0，立即执行）：**

- 所有文件写入改为 atomic write（先写 `.tmp` 再 `rename`）：
  ```python
  def write_long_term(self, content: str) -> None:
      tmp = self.memory_file.with_suffix(".tmp")
      tmp.write_text(content, encoding="utf-8")
      tmp.replace(self.memory_file)  # atomic on POSIX
  ```
- 同样适用于 `append_history()`、daily file 写入、observability JSONL 写入
- 理由：与策略优先级 `data safety` 最高一致，修改量小，风险低

DoD：

- 有一份可重复执行的 baseline test + observe 命令集
- golden output 测试覆盖核心写入链路
- 所有文件写入操作通过 atomic write 保护

R0 进展（2026-02-28）：

- 已落地 atomic write 基础能力：新增 `atomic_write_text()` / `atomic_append_text()`。
- 已接入 memory 主链写入路径：
  - `MemoryStore.write_long_term()` / `append_history()` / daily 写入 / routing+guard+sanitize+conflict 指标写入
  - `ContextBuilder` 的 `context-trace.jsonl` 追加写入
  - `memory_maintenance` 的 JSONL 追加与 cleanup 文件覆盖写入
- 已补充基础测试：`tests/test_atomic_file_io.py`
- 已补充 golden 回归最小样本：
  - `tests/fixtures/memory_golden/routing_missing_daily_sections.json`
  - `tests/fixtures/memory_golden/sanitize_recent_topic_section.json`
  - `tests/fixtures/memory_golden/guard_heading_retention_low.json`
  - 对应测试：`tests/test_memory_golden.py`（`pytest.mark.golden`）

R0 基线命令集（可重复执行）：

```bash
uv run pytest -q tests/test_atomic_file_io.py tests/test_memory_golden.py tests/test_memory_store_rules.py tests/test_consolidation_race.py tests/test_memory_maintenance.py tests/test_memory_observe_cli.py
uv run nanobot memory-observe --tag r0-baseline
uv run nanobot memory-audit --metrics-summary --guard-metrics-summary --sanitize-metrics-summary --conflict-metrics-summary --context-trace-summary
```

R0 状态：已完成（数据安全基线 + golden 回归基线）。

### Phase R1：模块拆分 + Pipeline 抽象（行为不变）

目标：仅重构结构，不变业务行为。引入 Pipeline 编排模式。

- 抽离 consolidation orchestration → `memory_consolidation.py`
- 抽离 routing policy → `memory_routing_policy.py`
- 抽离 guard policy → `memory_guard_policy.py`
- **[新增] 抽离文件 IO → `memory_io.py`**（含 atomic write）
- **[新增] 引入 `ConsolidationPipeline` 抽象**：
  - 将 `_apply_save_memory_tool_call` 拆解为 `NormalizeHistoryStep → RouteDailySectionsStep → SanitizeMemoryUpdateStep → GuardMemoryUpdateStep → WriteStep`
  - 每个 step 可独立单测、独立灰度开关、独立指标采集
- **[新增] 抽取 `JsonlMetricsSummarizer` 通用框架**：
  - 将 `memory_maintenance.py` 中重复的“JSONL 读取 → 解析 → 聚合 → summary → render markdown”模式提取为配置驱动
  - 预估可缩减 `memory_maintenance.py` 30-40%
- 保持 CLI 行为与指标字段兼容

DoD：

- 现有 memory 相关测试全通过
- R0 golden case 输出一致（history/daily/memory）
- `memory_maintenance.py` 中新增指标类型仅需配置不需代码

R1 进展（2026-02-28）：

- 已落地模块拆分（行为保持不变）：
  - consolidation 编排抽离到 `nanobot/agent/memory_consolidation.py`（`ConsolidationPipeline`）
  - daily 路由策略抽离到 `nanobot/agent/memory_routing_policy.py`
  - 文件 IO 抽离到 `nanobot/agent/memory_io.py`（统一 `MemoryIO` 写入入口）
- 已完成主链接线：
  - `MemoryStore` 通过 `self._pipeline` 执行 `save_memory` 应用流程
  - `ContextBuilder` 与 `memory_maintenance` 改用 `MemoryIO` 写入接口
- 已落地 guard policy 抽离：
  - `sanitize/guard/conflict` 逻辑抽离到 `nanobot/agent/memory_guard_policy.py`
  - `MemoryStore` 保留原方法签名，通过委托调用 policy（行为兼容）
- 已落地 `JsonlMetricsSummarizer` 最小框架：
  - 新增通用 JSONL 读取层（`JsonlMetricsSummarizer.load_rows`）
  - 已接入：`daily-routing` / `cleanup-stage` / `cleanup-conversion-index` 三类汇总
- 回归验证：
  - `tests/test_memory_store_rules.py`
  - `tests/test_consolidation_race.py`
  - `tests/test_memory_maintenance.py`
  - `tests/test_memory_observe_cli.py`
  - `tests/test_atomic_file_io.py`
  - `tests/test_memory_golden.py`
  - 当前结果：`111 passed`

R1 状态：已完成（模块拆分 + pipeline + guard policy + JSONL 汇总框架最小落地）。

### Phase R1.2：Memory Package 化（新增阶段）

目标：将 memory 管理代码聚合到单独逻辑包，降低 `agent` 目录耦合，同时保持外部行为不变。

实施策略（两步）：

- 第一步（现在执行）：逻辑包化，不拆 distribution
  - 新增 `nanobot/memory/` 包，承载 memory 主链模块：
    - `store.py`（原 `MemoryStore` facade）
    - `consolidation.py`
    - `routing_policy.py`
    - `guard_policy.py`
    - `io.py`
    - `maintenance.py`
  - 在 `nanobot/agent/` 下保留兼容 shim（薄转发导出），避免一次性改全仓 import
- 第二步（后续评估）：物理包独立（可选）
  - 仅在 API 边界稳定后，再评估独立 distribution（例如 `nanobot-memory`）

DoD：

- `nanobot/memory/` 成为 memory 代码主入口
- `nanobot/agent/*memory*` 仍可被旧路径 import（兼容 shim）
- memory 相关测试与 CLI 行为不变

R1.2 进展（2026-02-28）：

- 已新增 `nanobot/memory/` 逻辑包并迁移主模块：
  - `store.py`
  - `consolidation.py`
  - `routing_policy.py`
  - `guard_policy.py`
  - `io.py`
  - `maintenance.py`
- 已在 `nanobot/agent/` 保留兼容 shim：
  - `memory.py`
  - `memory_consolidation.py`
  - `memory_routing_policy.py`
  - `memory_guard_policy.py`
  - `memory_io.py`
  - `memory_maintenance.py`
- 验证结果：memory 主线回归 `111 passed`（行为保持一致）。

R1.2 状态：已完成（逻辑包化 + 兼容 shim）。

### Phase R1.5：Section-level Merge（新增阶段）

目标：消除 MEMORY.md 全文替换的数据丢失风险，不改存储格式。

- 实现 `parse_sections(md) -> SectionDict`：将 MEMORY.md 解析为 `Dict[heading, List[bullet]]`
- 实现 `section_merge(current, candidate) -> SectionDict`：以现有 section 为基础做增量合并
- 实现 `render_sections(sections) -> str`：序列化回 markdown
- 将 `_apply_save_memory_tool_call` 中的 `write_long_term(update)` 替换为 `write_long_term(render_sections(section_merge(current, candidate)))`
- `heading_retention_too_low` guard 从“拦截丢弃”升级为“自动修复后写入”

DoD：

- MEMORY.md 写入不再因 LLM 遗漏单个 section 而丢失全量更新
- 现有 golden case 输出一致或可解释差异（增量合并的预期变化）
- guard 拦截率显著下降（heading_retention 类不再触发）

R1.5 进展（2026-02-28）：

- 已实现 section-level merge 核心逻辑：
  - `parse_sections`（按 H2 切分）
  - `section_merge`（同名 section 增量合并 + bullet 去重）
  - `render_sections`（回写 markdown）
- 已接入 consolidation 主链：
  - `memory_update` 在 sanitize 后、guard 前执行 section merge
  - 对“仅返回部分 section”的候选更新，自动保留 current memory 既有 section
- 行为变化（与 R1.5 目标一致）：
  - 典型 `heading_retention_too_low` 场景由“guard 拒绝写入”升级为“merge 修复后 no-op / 正常写入”
- 回归：
  - 新增 section merge 专项测试（保留旧 section / 新增 section / 去重 / 非结构化回退）
  - memory 主线测试当前 `114 passed`

R1.5 状态：已完成（section-level merge 已落地并接入写入链路）。

### Phase R2：策略面收敛 + 中断恢复 + 指标基线

目标：把策略开关与策略结果显式化，补全数据安全短板。

- 完整落地 `compatible/preferred/required` 语义文档与指标
- 给 `preferred` 增加明确收益指标门槛
- 引入 P0/P1/P2 最小打标（先打在 conversion/compact/ttl 指标层，不改存储格式）
- **[新增] Consolidation 中断恢复**：
  - chunk 循环开始前写入 `consolidation-in-progress.json`（含 scope 信息）
  - 每处理完一个 chunk 更新 progress
  - 下次 consolidation 启动时检查此文件，如有未完成的 scope 则恢复
  - 比 R3 的 run_id 恢复更轻量，但解决最核心的中断安全问题
- **[新增] Guard/Sanitize 指标补全"正常通过"基线**：
  - 新增 `memory-update-outcome.jsonl`，每次 memory_update 处理（无论是否被拦截/清洗）都写入一行：
    ```json
    {"ts": "...", "session_key": "...", "outcome": "written|guard_rejected|sanitize_modified|no_change|truncated_skip", "guard_reason": null, "sanitize_changes": 0}
    ```
  - Phase Gate 的升级决策可直接基于 `outcome` 分布做自动判定
- **[新增] Preference Conflict 检测扩展**：
  - 将 preference key 注册为配置而非代码常量
  - 引入 conflict resolution strategy：`keep_old` / `keep_new` / `ask_user` / `merge`
  - 对于 `ask_user` 策略，在 consolidation 后向下一次用户交互注入确认提示

DoD：

- `preferred` 升级决策可由指标自动判定（见 Phase Gate）
- 打标数据可用于 cleanup/ttl 报告分层统计
- consolidation 中断后下次启动可自动恢复
- `outcome` 指标覆盖所有 memory_update 处理路径

### Phase R2.5：L1 Insights 中间层（新增阶段）

目标：填补"半持久知识"的存储空白。

动机：
- 当前 MEMORY.md 要求"只存 durable facts"，HISTORY.md 只是流水日志
- 存在一类信息既非瞬时事件也非永久事实：技术方案 trade-off 分析、调试关键发现、lessons learned
- 这类信息放 MEMORY.md 会被 sanitize 清洗（含日期/技术细节），放 HISTORY.md 又不够结构化

实施：
- 新增 `memory/INSIGHTS.md`，定位为"半持久 lessons learned"
- TTL 30-90 天（可配置），section 按主题分类
- 格式与 daily 类似但生命周期更长
- consolidation prompt 增加 `insights_update` 可选字段
- `memory-audit` 增加 insights TTL 清理支持

DoD：

- INSIGHTS.md 可正常读写，TTL 清理可观测
- consolidation 可选产出 insights_update

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
- **[新增] Daily Recall 意图判定升级**：
  - 短期：扩展关键词列表（补充"上周"、"昨天"、"前天"等），加入否定排除规则
  - 中期：用一个小的 classifier（可复用 consolidation LLM provider）做轻量意图判断，返回 `(should_recall: bool, confidence: float)`
  - 在 `context-trace.jsonl` 中记录 recall 触发原因和是否命中，用于 recall 质量评估
- 对 recent daily 注入做 section/age/token 三维预算
- 在 dashboard 输出 recall 命中代理指标

DoD：

- 回顾类 query 的成功率提升，默认 token 开销不显著上升
- recall 触发精度有可衡量指标（触发率 + 命中率）

### Phase R5：数据模型升级（谨慎）

目标：在不破坏 markdown 兼容的前提下引入结构化元数据。

- 为 daily/history 引入轻量元信息（id/source/run/tag）
- MEMORY 仍可保持 markdown 主体，但引入可解析边注格式
- 基于 R1.5 的 SectionDict 基础，为 section 增加 metadata（last_updated、source_session 等）

DoD：

- 保持旧数据可读可写
- 新旧版本共存下行为可预测

## 7. Phase Gate（升级门槛）

从 `compatible -> preferred` 的建议门槛（7 天窗口）：

- consolidation 样本量 `N >= 50`（避免小样本波动）
- `structured_daily_ok_rate >= 75%`
- `fallback_missing_rate` 连续下降
- `preferred_retry_used` 占比不高且有效提升 `tool_call_has_daily_sections`
- guard/sanitize 不出现明显回归
- **[新增]** `memory-update-outcome` 中 `written` 占比 >= 80%（基于 R2 新增的 outcome 指标）

从 `preferred -> required` 的建议门槛（14 天窗口）：

- consolidation 样本量 `N >= 100`（且覆盖多个活跃 session）
- `tool_call_has_daily_sections_rate >= 95%`
- required 灰度会话中无明显 recall 退化
- fallback 相关告警可控
- **[新增]** `guard_rejected` 占比 < 5%（排除 `heading_retention` 类，R1.5 后该类应近零）

## 8. 测试策略

- 单元测试：
  - policy 决策函数（表驱动）
  - guard/sanitize 边界样本
  - lifecycle/ttl/restore 数据路径
  - **[新增] section_merge 合并逻辑**：section 新增、合并、去重、空 section 处理
  - **[新增] Pipeline step 独立测试**：每个 step 的输入/输出/跳过条件
  - **[新增] atomic write 异常测试**：模拟写入中断后文件完整性验证
- 集成测试：
  - consolidate 端到端（tool call 字符串/对象参数、错误返回）
  - memory-audit CLI 关键命令组合
  - **[新增] consolidation 中断恢复**：模拟 chunk 间中断后重启恢复
- 回归测试：
  - 基线样本 golden output 比对（R0 固化，`tests/fixtures/memory_golden/`）
  - **[新增] R1.5 前后 MEMORY.md 写入行为差异的可解释性验证**

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
4. **[新增]** Section-level merge 在 heading 冲突时的策略：保留两者 bullets（union）还是以 candidate 为准（override）。建议默认 union，仅在 candidate 明确标记 `[replace]` 时 override。
5. **[新增]** Preference conflict 的 `ask_user` 策略如何注入到下次交互：system prompt 注入 vs 独立消息。建议 system prompt 注入以减少打扰感。

### 已决策事项（记录）

1. 在 R2.5 引入 `memory/INSIGHTS.md` 作为 L1 中间层，定位半持久 lessons learned（TTL 30-90 天），填补 MEMORY（长期）与 HISTORY（流水）之间空档。

## 11. 改进建议优先级汇总

按策略优先级 `data safety > traceability > interruption safety > recall quality > token cost` 排列：

| 优先级 | 改进项 | 匹配原则 | 落地阶段 |
|--------|--------|----------|----------|
| P0 | Atomic write（写入原子化） | data safety | R0（立即） |
| P0 | Consolidation 中断恢复 | interruption safety | R2 |
| P1 | Section-level merge（增量更新） | data safety + recall | R1.5 |
| P1 | Guard/Sanitize outcome 基线指标 | traceability | R2 |
| P1 | Golden output 自动化回归 | 重构安全网 | R0 |
| P2 | Pipeline 抽象 | 可维护性 | R1 |
| P2 | JsonlMetricsSummarizer 框架 | 可维护性 | R1 |
| P2 | L1 Insights 中间层 | recall quality | R2.5 |
| P3 | Daily Recall 意图分类升级 | recall quality | R4 |
| P3 | Preference conflict 扩展 | recall quality | R2 |

## 12. 本文档维护规则

- 文档只保留“当前状态 + 可执行计划”，不再累积冗长流水日志。
- 每个阶段完成后仅更新：
  - 状态
  - 关键行为变更
  - 指标与 gate 结论
  - 下一阶段决策
