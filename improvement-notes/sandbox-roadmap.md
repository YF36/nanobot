# nanobot 沙箱抽象层路线图（S1-S4）

本文件聚焦 nanobot 的工具执行沙箱化设计，目标是在不牺牲当前可用性的前提下，为 `exec` 等高风险工具引入可插拔的沙箱执行后端（如 Docker / gVisor / boxlite）。

设计参考：

- `A thousand ways to sandbox an agent`（[michaellivs.com](https://michaellivs.com/blog/sandbox-comparison-2026/)）
- nanobot 当前 `ExecTool` / `ShellExecutor` 结构（已完成 guard/executor/formatter 拆分）

核心判断（基线）：

- 抽象层应以 **安全策略 + 能力声明** 为核心，不只是“命令执行接口”。
- 先覆盖 `exec` 工具，再评估文件工具的隔离方案。
- 先支持可选后端并保留本地路径，避免一开始强依赖 Docker 或重型隔离实现。

---

## 背景与目标

### 当前状态（nanobot）

- `exec` / shell 工具有较强应用层 guard（regex / shlex / workspace path），但仍在宿主机执行。
- 文件工具（`read/write/edit`）有路径与 symlink 防护，但对工作区的读写仍是“真实落盘”。
- 当前主要风险控制位于应用层；缺少“执行环境隔离”层。

### 为什么现在讨论沙箱抽象层

- 风险最高的工具（`exec`）已经有清晰执行边界（`ShellExecutor`），适合作为抽象切入口。
- nanobot 当前迭代风格是“小步、可观测、可回退”，适合先做 `S1` 设计与接口边界，不直接上重型方案。
- 未来可能对接不同后端（`docker` / `gvisor` / `boxlite`），抽象层可以避免实现锁定。

### 非目标（当前路线不做）

- 不在 `S1/S2` 阶段实现“全工具统一沙箱”。
- 不在 `S1/S2` 阶段引入企业级多租户沙箱编排系统。
- 不在 `S1` 阶段承诺统一支持所有后端能力（会通过能力声明做降级）。

---

## 设计原则（来自文章与 nanobot 现状）

1. 安全策略优先于后端选择
- 抽象层必须显式描述：网络、文件系统、超时、资源限制、环境变量策略。
- 避免把“docker 参数细节”直接泄漏到上层工具逻辑。

2. 能力声明优先于隐式假设
- 不同后端能力差异大（如网络开关、只读 rootfs、挂载、资源限制）。
- 上层通过能力探测决定策略降级，而不是假设“所有后端都支持同样功能”。

3. 兼容当前行为（默认本地）
- 第一阶段必须保留 `LocalSandboxRunner`（本质是当前本地执行路径的适配器）。
- 默认行为不变，沙箱执行后端通过配置启用。

4. 渐进式复杂度
- 先做 `run-once`（一次命令一次执行环境），不先做长生命周期容器复用。
- 先 `exec`，后文件工具；先后端抽象，后策略路由。

5. 可观测性内建
- 沙箱相关日志/事件字段必须统一（backend / profile / network / latency / timeout）。
- 便于后续基于数据评估是否切换默认后端或强化策略。

---

## 总体方案概览

### 分层结构（建议）

- `ExecTool`
  - 参数校验、用户语义、结果格式化（已有）
- `ShellExecutor`
  - 面向 `exec` 的执行编排
  - 调用 `SandboxRunner`
- `SandboxRunner`（新抽象）
  - 统一执行接口
  - 统一策略对象
  - 统一结果对象
- `SandboxBackend` 实现（后续）
  - `LocalSandboxRunner`（兼容当前）
  - `DockerSandboxRunner`
  - `GvisorSandboxRunner`（可能通过 OCI/runtime 接入）
  - `BoxliteSandboxRunner`（SDK/API 适配）

### 关键对象（概念）

- `SandboxPolicy`
  - `network_mode`
  - `filesystem_policy`
  - `timeout_s`
  - `resource_limits`
  - `env_policy`
- `SandboxCapabilities`
  - 声明后端支持的能力
- `SandboxRunRequest`
  - 命令执行请求（cmd/cwd/env/profile 等）
- `SandboxRunResult`
  - 执行结果（exit/stdout/stderr/timed_out/backend/metadata）

---

## S1：抽象层与能力声明（先设计后实现）

### S1 目标

- 定义 `SandboxRunner` 抽象与策略/结果对象。
- 定义能力声明 `SandboxCapabilities`。
- 明确默认兼容行为（本地执行）和降级规则。
- 不改 `ExecTool` 外部接口，不引入具体沙箱依赖。

### S1 范围（建议）

1. 抽象接口（面向命令执行）
- 输入应覆盖：
  - 命令（字符串或 argv）
  - `cwd`
  - `env`
  - `timeout_s`
  - `policy`
- 输出应覆盖：
  - `exit_code`
  - `stdout` / `stderr`
  - `timed_out`
  - `duration_ms`
  - `backend`
  - `metadata`

2. 安全策略对象（重点）
- `network_mode`
  - `off` / `restricted` / `on`
- `filesystem_policy`
  - `workspace_mode`（例如只读 / 读写）
  - `writable_paths`
  - `readonly_paths`
  - （可选）`tmpfs_paths`
- `env_policy`
  - `inherit_env`（bool）
  - `allowed_env_keys` / `denied_env_keys`
- `resource_limits`
  - `cpu`
  - `memory_mb`
  - `pids`

3. 能力声明（必须）
- 示例能力：
  - `supports_network_mode`
  - `supports_bind_mounts`
  - `supports_readonly_rootfs`
  - `supports_resource_limits`
  - `supports_env_filtering`
  - `supports_streaming_output`
- 上层行为：
  - 若策略要求某能力但后端不支持：
    - 安全优先模式：拒绝执行
    - 兼容模式：降级并记录 warning（默认建议仅在本地开发使用）

4. 观测字段基线（与现有日志规范化对齐）
- `sandbox_backend`
- `sandbox_policy_name`（或 profile）
- `network_mode`
- `sandbox_timeout_s`
- `sandbox_timed_out`
- `sandbox_latency_ms`
- `sandbox_degraded`（bool）
- `sandbox_degrade_reason`

### S1 设计注意点

- 不把 Docker/OCI 参数直接暴露给 `ExecTool`。
- `SandboxRunner` 不负责 shell guard；guard 仍在 `ExecTool` / `ShellGuard` 层。
- 抽象层只解决“在哪里执行、以什么策略执行”。

### S1 验收标准（设计完成标准）

- 能用一页接口定义清楚描述 `SandboxRunner`、`SandboxPolicy`、`SandboxCapabilities`、`SandboxRunResult`。
- 能说明“后端能力不足时”的行为（拒绝/降级）策略。
- 能与当前 `ExecTool -> ShellExecutor` 结构自然对接。

---

## S2：后端实现（Local + Docker）与 `exec` 接入（最小可用）

### S2 目标

- 先完成可运行的双后端：
  - `LocalSandboxRunner`（兼容当前）
  - `DockerSandboxRunner`（可选启用）
- `exec` 工具可通过配置选择 backend。
- 默认行为保持与当前一致（仍可本地执行）。

### S2 范围（建议）

1. `LocalSandboxRunner`
- 本质是对现有本地执行路径的适配。
- 负责：
  - 将 `SandboxPolicy` 映射到当前可支持能力（超时、环境变量过滤等）
  - 对不支持能力给出 `degraded` 标记
- 目的：
  - 先打通抽象，不改变体验。

2. `DockerSandboxRunner`（最小版）
- 采用 `docker run --rm` 的 `run-once` 模式。
- 优先支持：
  - workspace bind mount（可配只读/读写）
  - `--network none`（对应 `network_mode=off`）
  - 超时（由上层和 docker 进程双重控制）
  - 基础资源限制（如 `--memory`, `--cpus`，按需）
- 暂不做：
  - 容器池复用
  - 镜像动态构建/复杂预热
  - 多命令会话状态持久化

3. `ExecTool` 接入策略
- `ExecTool` 新增 sandbox 配置项（例如 backend/profile），但保留默认本地。
- `ShellExecutor` 改为通过 `SandboxRunner` 执行。
- `ShellOutputFormatter` 保持不变（结果格式化层无需知道后端细节）。

4. 观测（必须）
- 在 `exec` 执行日志中记录：
  - `sandbox_backend`
  - `sandbox_timed_out`
  - `sandbox_latency_ms`
  - `sandbox_degraded`
- 与已有 `exec` 结构化 `details` 兼容：
  - 可选在 `details` 中增加 `sandbox_backend` / `network_mode`

### S2 风险与缓解

- 用户未安装 Docker：
  - 启用 `docker` backend 时显式报错，不静默回退（安全优先）
- 路径映射不一致：
  - 统一要求 `cwd` 在 workspace 内；超出则拒绝
- 输出兼容性：
  - 维持当前 `exec` 文本输出格式，避免影响 LLM 行为

### S2 验收标准

- 不开沙箱配置时，`exec` 行为与现状一致。
- 开启 `docker` backend 后，能执行简单只读命令并返回兼容输出。
- `network_mode=off` 等基础策略可生效（至少在 Docker backend）。

---

## S3：扩展后端与策略路由（gVisor / boxlite / profile）

### S3 目标

- 在不改上层 `ExecTool` 调用语义的前提下，扩展更多后端。
- 引入 profile 化策略，降低配置复杂度。

### S3 范围（建议）

1. Profile 抽象（推荐）
- 预定义策略 profile（示例）：
  - `safe-readonly`
  - `workspace-write`
  - `networked-dev`
  - `high-isolation`
- profile -> `SandboxPolicy` 映射由配置驱动（后续可逐步外露）。

2. `gVisor` / `boxlite` 适配
- 通过 `SandboxCapabilities` 做能力对齐。
- 重点不是“支持全部功能”，而是先支持 `exec` 核心路径：
  - 运行命令
  - 读写指定目录（或挂载）
  - 网络开关（若支持）
  - 超时与退出码

3. 失败与回退策略（明确化）
- 配置项建议支持：
  - `fallback_to_local_on_backend_unavailable`（默认 `false`）
- 安全优先场景：
  - 后端不可用 -> 直接失败
- 个人开发调试场景（可选）：
  - 后端不可用 -> 回退本地并打 warning

4. 工具级策略路由（仅限 `exec`）
- 先不做“全工具路由”
- 可根据命令类型/配置选择 profile（例如默认只读 profile，显式允许写入时切换）

### S3 验收标准

- 新增后端不会要求修改 `ExecTool` 主逻辑（仅扩展 runner 注册/选择）。
- profile 能覆盖大多数常见 `exec` 场景，而不用用户手写大量低层参数。
- 能通过能力声明稳定降级，不出现“后端 silently 忽略安全策略”的情况。

---

## S4：观测、安全基线与后续扩展（稳定化阶段）

### S4 目标

- 将沙箱执行纳入 nanobot 的统一观测体系。
- 建立可比较的后端质量基线（延迟、失败率、超时率、降级率）。
- 为后续扩大到其他工具（文件工具/网络工具）提供依据。

### S4 范围（建议）

1. 统一观测字段（延续当前规范化工作）
- 日志/事件统一字段建议：
  - `sandbox_backend`
  - `sandbox_profile`
  - `sandbox_network_mode`
  - `sandbox_latency_ms`
  - `sandbox_timed_out`
  - `sandbox_success`
  - `sandbox_error_kind`
  - `sandbox_degraded`
  - `sandbox_degrade_reason`

2. 汇总指标（先日志 summary，不必先上 OTEL）
- 类似 `/new background archival summary` 的模式，可先做周期汇总：
  - `sandbox_exec_summary`
  - `by_backend`
  - `timeout_rate`
  - `degrade_rate`
  - `avg_latency_ms`
- 后续再决定是否接 OTEL/metrics backend。

3. 审计与安全事件
- 对关键场景记录明确安全日志：
  - 策略拒绝执行（能力不足、越权写目录、网络策略不满足）
  - 后端回退（如果开启回退）
  - 运行时超时/异常分类

4. 扩展评估（是否进入文件工具）
- 基于 S2/S3 实际数据评估：
  - `exec` 沙箱是否稳定可用
  - 是否值得把文件工具引入“虚拟工作区/overlay”方案
- 不建议在没有运行数据前直接推进文件工具沙箱化。

### S4 验收标准

- 能从日志中回答：
  - 哪个 backend 最稳定？
  - 哪类命令最容易超时/失败？
  - 安全策略是否经常被降级？
- 有足够数据决定：
  - 是否提升某后端为默认
  - 是否继续扩展到文件工具

---

## 与 nanobot 现有路线图的关系

### 与 `architecture-roadmap.md` 的关系

- 本文是 `Filter Chain / StateStorage / FailoverPolicy` 之外的另一条安全与执行隔离主线。
- 建议作为 `Phase 4` 之前的专题设计，不与当前 `M2/M3` 记忆线冲突。

### 与当前已实现能力的关系

- 复用：
  - `ExecTool` 的 guard 分层（`ShellGuard`）
  - `ShellExecutor` 拆分基础
  - 结构化工具结果 `details`
  - 观测字段规范化工作（可直接延续）
- 不替代：
  - 应用层 guard 仍保留（沙箱是第二道防线，不是替代）

---

## 当前建议（执行策略）

1. 先停在设计阶段（S1-S4 文档化）
- 暂不立即实施代码
- 先确认：
  - 目标后端优先级（Docker 是否先做）
  - 本机环境支持情况
  - 安全优先 vs 兼容回退策略

2. 真正开始实施时，按顺序推进
- `S1` 抽象 + 能力声明
- `S2` Local/Docker + `exec` 接入
- `S3` profile 与更多后端
- `S4` 观测汇总与数据驱动决策

3. 始终保持“小步可回退”
- 默认行为不变
- 新能力通过配置开关启用
- 先做 `exec`，后评估其他工具
