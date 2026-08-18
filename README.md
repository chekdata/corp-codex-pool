# corp-codex-pool

把多个 Codex 订阅变成一个内部号池，让 [Multica](https://github.com/multica-ai/multica) 任务按人、按任务使用和审计公司额度。

**CHEK 正式环境提供两条受管路径：Multica 任务使用运行期绑定凭证；员工也可为官方 Codex GUI 申请无固定到期日、但可即时撤销且会话自动同步的个人凭证。**

```
codex   →  provider: openai  →  你的个人订阅
mcodex  →  provider: gw      →  公司号池网关  →  Pro 账号 A / B / N
```

同一台机器、同一个模型，走不同上游，互不干扰。

---

## 目录

- [解决什么问题](#解决什么问题)
- [工作原理](#工作原理)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令参考](#命令参考)
- [配置项](#配置项)
- [两种接入方式](#两种接入方式)
- [密钥管理](#密钥管理)
- [用量与计量](#用量与计量)
- [故障排查](#故障排查)
- [已知限制](#已知限制)
- [实测数据](#实测数据)
- [设计取舍](#设计取舍)
- [开发](#开发)
- [FAQ](#faq)

---

## 解决什么问题

企业买了多个 Codex 订阅想分给工程师用，会遇到三个问题：

1. **不能直接分发账号。** 账号凭据散落到每台机器上，人一走就要改密码，用量也无从归集。
2. **号池网关知道花了多少，但不知道是谁花的。** 网关只看得见 API 密钥。
3. **multica 知道人、任务、工作区，但它不做 LLM 代理。** 它的路由代码里写得很明白，暴露一个通用 LLM 代理会让任何登录用户拿部署方的密钥跑任意补全。

于是两边各跑各的：**网关有量无人，multica 有人无量**。

这个工具把两边接上：

| 能力 | 只有网关 | 只有 multica | 接上之后 |
|---|---|---|---|
| 多个订阅共享 | 有 | 无 | 有 |
| 按人/任务计量 | 做不到（只看得见密钥） | 无请求级用量 | 临时 Key 名绑定用户与任务，可归集 |
| 超额硬拦截 | 可做 | 物理上做不到<sup>1</sup> | 网关拦截 |
| 员工无需管理 Key | — | — | 任务启动时自动签发，不落盘 |
| 防止脱离公司任务使用 | 无 | 有任务上下文 | 缺任务票或上下文不匹配即拒绝 |

<sup>1</sup> multica 的用量只在任务结束后上报一次，跑到一半发现超额没法掐断。

### 适用场景

- 团队有多个 Codex 订阅要共享，需要知道每个人用了多少
- 用 multica 做多 agent 编排，希望 agent 跑的 codex 统一走公司额度
- 需要按人设额度上限、模型白名单、有效期
- 人员流动时要能立即回收访问权

### 不适用

- 只有一个人用 —— 直接用 codex 就好
- 想绕过订阅条款做转售 —— 本项目是给企业内部分发自购订阅用的

---

## 工作原理

`mcodex` 是个薄封装：准备好环境后用 `execvp` 交棒给真 codex，argv 与 stdio 原样透传。

```mermaid
flowchart LR
    U[员工] -->|mcodex| M[mcodex]
    S[multica server] -->|派单| D[multica daemon]
    D -->|MULTICA_CODEX_PATH| M
    M -->|execvp<br/>argv/stdio 原样透传| C[codex]
    C -->|POST /v1/responses<br/>Authorization: Bearer| G[codex-lb 网关]
    G --> A1[Pro 账号 A]
    G --> A2[Pro 账号 B]
    G --> A3[Pro 账号 N]
    G -.per-request 计量.-> DB[(用量表)]
```

Multica daemon 拉起的任务仍走 `mcodex`：daemon 注入 `CODEX_HOME` 与任务上下文，网关在每次请求时确认 Run 仍处于活动状态，Run 结束后凭证立即失效。官方 Codex GUI 则使用员工在 Multica 自助生成的 `mck_` 凭证，每次请求回查工作空间成员与密钥状态，并把会话同步回 Multica。

### 依赖的三个上游机制

全是 codex 与 multica 本来就有的行为，没有一处 hack：

| 机制 | 作用 |
|---|---|
| codex 自定义 provider 的 `env_key` 从环境变量读密钥，且**优先于 `auth.json`** | 本次任务注入临时凭证；不需要 `codex login` |
| multica daemon 支持 `MULTICA_CODEX_PATH` 指定 codex 可执行文件 | 不改 multica 就能换成 `mcodex` |
| Multica 的 `mat_` 任务票在服务端绑定用户、Agent、任务和 Workspace | 网关不信任客户端自报身份，只接受服务端证明 |

### 密钥注入时序

```mermaid
sequenceDiagram
    participant D as multica daemon
    participant M as mcodex
    participant C as codex
    participant G as 网关

    D->>D: 准备 per-task CODEX_HOME<br/>拷贝宿主 config.toml
    D->>M: exec mcodex app-server --listen stdio://<br/>env: CODEX_HOME, MULTICA_TASK_ID…
    M->>M: 往 per-task config.toml 注入 [model_providers.gw]
    M->>G: 用 mat_ 任务票申请短时凭证
    G->>S: 校验服务端绑定的用户/Agent/任务/Workspace
    G-->>M: 返回加密 mcx_ 凭证（不含可见原始 Key）
    M->>C: execvp（argv 与 stdio 原样透传）
    C->>G: POST /v1/responses<br/>Authorization: Bearer GW_API_KEY
    G->>G: 按密钥鉴权 + 配额检查 + 路由到某个账号
    G-->>C: SSE 流
    G->>G: 请求完成后记账（模型/token/成本/延迟）
```

---

## 安装

需要 **Python 3.11+**（用到标准库 `tomllib`）。

```bash
git clone https://github.com/chekdata/corp-codex-pool.git
cd corp-codex-pool
pip install -e .
```

装完会有两个命令：

- `poolctl` —— 管理端（签发密钥、看用量、体检）
- `mcodex` —— 员工端（走号池的 codex）

### 前置条件

| 组件 | 要求 |
|---|---|
| [codex CLI](https://github.com/openai/codex) | 已安装。`mcodex` 会 exec 它 |
| 号池网关 | 必须实现 **Responses API**（`POST /v1/responses`）。本项目针对 [codex-lb](https://github.com/Soju06/codex-lb) 开发与验证 |
| Multica | CHEK 正式环境必需；员工先登录并在本机运行 daemon |

> ⚠️ **网关必须支持 Responses API。** codex 已经移除了 `wire_api = "chat"`，只接受 `"responses"`。只做 chat/completions 兼容的网关**接不上**。

### CHEK 正式维护方式

CHEK 内部正式方案按四层维护：

- `chekdata/multica`：提供任务票据的服务端绑定证明
- `chekdata/codex-lb`：正式托管号池网关源码、CI 与镜像
- `chekdata/corp-codex-pool`：正式托管员工侧 `poolctl` / `mcodex`
- `chekdata/ops-bootstrap`：正式托管 prod ArgoCD 发布清单

也就是说，**以后功能开发都在 CHEK 自己的 fork 上做**，不再直接改 upstream 仓库；上线镜像和 prod 发布也都走 CHEK 自己的仓库与流水线。

---

## 快速开始

### 员工：一次性接入

```bash
multica login
poolctl enroll
```

`poolctl enroll` 会初始化独立的 `~/.mcodex`、清理旧永久 Key，并用 `mcodex` 重启 Multica daemon。之后员工只需在 `multica.chekkk.com` 创建任务，不需要看见、粘贴或保存 API Key。

### 使用

在 Multica 中创建/继续一个 Codex 任务。任务的每轮消息、工具结果和状态由 Multica 原生任务窗口保存；网关按临时 Key 记录模型、token、成本和延迟。脱离 Multica 直接运行 `mcodex` 会被拒绝。可用 `poolctl daemon status` 验证运行入口，输出应包含：

```
✓ daemon 使用的 codex：/…/mcodex（走号池）
```

### 官方 Codex GUI / CC Switch

1. 飞连登录 Multica，进入 `设置 → Tokens → 公司 Codex 访问`。
2. 点击“创建访问”。推荐直接点“在 CC Switch 中打开”，确认导入后重启官方 Codex GUI。
3. 不使用 CC Switch 时，安装本仓库后运行 `poolctl gui configure`，按隐藏提示粘贴凭证，再重启官方 Codex GUI。

该命令只更新官方 Codex 的 `config.toml` 与 `auth.json`，保留已有官方登录字段，不安装启动器。凭证没有固定过期时间；轮换、主动撤销或被移出工作空间后会立即失效。GUI 中的提问、回复、模型、Token 与时间会同步到 Multica，禁止用于非公司工作。

### 验证

```bash
poolctl doctor
```

检查覆盖网关连通性、配置、任务绑定凭证模式和 Multica 登录状态。每个失败项都带可执行的下一步。

---

## 命令参考

### `poolctl enroll` — 员工一键接入

```bash
poolctl enroll [--no-inherit] [--real-codex <path>] [--no-restart-daemon]
```

不接收 Key。它只准备 `mcodex` 并确保 daemon 通过该入口启动；凭证在每个任务开始时自动签发。

### `poolctl issue` — 签发密钥

这是兼容私有/手工部署的管理员命令。CHEK prod 员工路径不使用它，也不向员工下发其结果。

```bash
poolctl issue <员工标识> [选项]
```

密钥名会是 `pool-<员工标识>`，建议用工号或邮箱前缀。

| 选项 | 说明 |
|---|---|
| `--agent-id <uuid>` | 同时下发到该 multica agent 的 `custom_env` |
| `--workspace-id <uuid>` | multica workspace，默认取本机 CLI 配置 |
| `--weekly-token-limit <n>` | 周令牌上限，超出返回 429 |
| `--allowed-model <name>` | 模型白名单，可重复。⚠️ 见下方警告 |
| `--expires-days <n>` | 有效期天数 |
| `--traffic-class <foreground\|opportunistic>` | 流量等级。`opportunistic` 只在池内有余量时才被接纳 |
| `--no-reuse` | 同名密钥已存在时报错而非复用 |
| `--dry-run` | 只显示将要做什么 |

> ⚠️ **模型白名单会放大重试。** 命中白名单外的模型时网关返回 403，而 codex 对 403 会**重试 6 次**。请求都被拦在网关、不消耗上游额度，但会放大网关负载。白名单务必覆盖员工日常使用的模型。

### `poolctl deliver` — 下发密钥到 agent

```bash
poolctl deliver --agent-id <uuid> --key <明文> [--dry-run]
```

把密钥写入指定 multica agent 的 `custom_env`。适合密钥已签发、事后补下发的情况。

### `poolctl revoke` — 吊销

```bash
poolctl revoke <员工标识> [--agent-id <uuid>] [--yes]
```

网关侧**立即生效**（实测 0.06 秒）。带 `--agent-id` 会同时清除该 agent 的 `custom_env`。

### `poolctl usage` — 用量

```bash
poolctl usage [--since ISO8601] [--until ISO8601] [--by-session] [--json]
```

默认按密钥归集：

```
密钥                       请求        输入        缓存   命中率   延迟中位     最慢    成本USD
--------------------------------------------------------------------------------------
pool-zhangsan                22   254,863   126,208    49.5%     3.5s     1.1m     0.6513
pool-lisi                    14    75,818         0     0.0%     6.1s     8.2s     0.0766
```

`--by-session` 按会话归集，看单次任务花了多少。

最慢请求超过 10 分钟时会自动告警 —— 那意味着 multica 的任务可能被误杀，详见[故障排查](#任务失败但网关显示成功)。

### `poolctl doctor` — 体检

```bash
poolctl doctor [--key <明文>] [--config <path>] [--json]
```

不带 `--key` 只做静态检查；带上则会真实发一次请求。检查项包括：

- 网关 Responses 端点是否存在、`/models` 是否实现
- 当前接入方式（mcodex / 宿主注入）与配置文件位置
- `wire_api` / `base_url` / `env_key` / 默认 provider / 对账请求头
- mcodex 家目录的密钥来源与固化的 codex 路径是否仍可用
- 密钥可用性、额度预警头、超限契约
- multica 连通性与 agent 密钥来源

### `poolctl status` — 概况

显示当前配置（凭据打码）与号池账号、密钥列表。

### `poolctl setup` / `unsetup` — 宿主注入方式

```bash
poolctl setup [--dry-run] [--no-default] [--no-headers] [--base-url URL]
poolctl unsetup [--dry-run]
```

把号池 provider 直接写进宿主 `~/.codex/config.toml`。**这会让你自己的 `codex` 也走号池**，除非确实想要整机切换，否则用 `mcodex`。详见[两种接入方式](#两种接入方式)。

幂等、自动备份，`unsetup` 可完整回滚。

### `poolctl mcodex init` — 初始化员工端

底层初始化命令；CHEK prod 员工优先使用 `poolctl enroll`。`--key` 选项仅保留给私有/手工部署。

```bash
poolctl mcodex init [--key <明文> | --key-stdin] [--inherit/--no-inherit]
                    [--home <path>] [--real-codex <path>]
```

| 选项 | 说明 |
|---|---|
| `--key-stdin` | 从 stdin 读密钥，避免出现在 shell 历史与 `ps` |
| `--no-inherit` | 不从 `~/.codex/config.toml` 继承偏好 |
| `--real-codex` | 指定真 codex 绝对路径。不给则自动探测并固化 |
| `--home` | 自定义家目录，默认 `~/.mcodex` |

### `poolctl mcodex path` — 打印 mcodex 路径

用于配置 `MULTICA_CODEX_PATH`。

### `poolctl daemon` — 带号池配置管理 daemon

```bash
poolctl daemon start
poolctl daemon restart
poolctl daemon status
```

multica 只能通过 `MULTICA_CODEX_PATH` 环境变量指定 codex 路径（它的 `config.json` 只支持 `server_url` / `app_url` / `workspace_id` 三个键）。直接 `multica daemon start` 拿不到这个变量，daemon 会**悄悄退回系统 codex、绕开号池**。

> ⚠️ **别用 `multica daemon restart` 代替 `poolctl daemon restart`。** 前者由已在运行的 daemon 自己拉起新进程，会继承旧环境，新设的变量传不进去。`poolctl daemon restart` 是 stop 再 start。

---

## 配置项

配置优先级：**命令行参数 > 环境变量 > `.env` 文件 > 默认值**。

### 管理端（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `POOL_ADMIN_URL` | `https://pool.chekkk.com` | 网关管理面地址（给 `poolctl` 这类管理流量直连 `codex-lb`） |
| `POOL_ADMIN_PASSWORD` | — | 管理面密码。codex-lb 用 cookie session 登录 |
| `POOL_ADMIN_TOKEN` | — | 预留，当前管理面不接受 Bearer |
| `POOL_BASE_URL` | `https://codex.chekkk.com/v1` | 员工侧 codex 访问的网关地址。**必须是 daemon 宿主机可达的地址** |
| `POOL_SESSION_URL` | `https://codex.chekkk.com/api/self-service/session-key` | Multica 任务票换取短时号池凭证的地址 |
| `MULTICA_SERVER_URL` | `https://api.multica.ai` | 自托管填自己的地址 |
| `MULTICA_TOKEN` | — | 不填则读 `~/.multica/config.json` |
| `POOL_PROVIDER_ID` | `gw` | 出现在 `[model_providers.<id>]` |
| `POOL_ENV_KEY` | `GW_API_KEY` | codex 从此环境变量读密钥，须与注入的 `env_key` 一致 |

### 员工端（环境变量）

| 变量 | 说明 |
|---|---|
| `GW_API_KEY` | 仅在 Codex 进程内存在的 `mcx_` 任务绑定凭证；正式模式不持久化 |
| `MCODEX_HOME` | 覆盖默认家目录 `~/.mcodex` |
| `MCODEX_REAL_CODEX` | 临时覆盖真 codex 路径，便于排障与灰度 |

### 注入到 codex 的配置长这样

```toml
# BEGIN corp-codex-pool (managed; do not edit by hand)
model_provider = "gw"

[model_providers.gw]
name = "Company Codex Pool"
base_url = "https://codex.chekkk.com/v1"
wire_api = "responses"
env_key = "GW_API_KEY"
env_key_instructions = "由号池控制台下发，请勿手工设置"

[model_providers.gw.env_http_headers]
"X-Multica-Task-Id" = "MULTICA_TASK_ID"
"X-Multica-Agent-Id" = "MULTICA_AGENT_ID"
"X-Multica-Workspace-Id" = "MULTICA_WORKSPACE_ID"
# END corp-codex-pool
```

字段名逐字对应 codex 的 `ModelProviderInfo`，不要臆造。

---

## 两种接入方式

| | `mcodex`（CHEK prod） | `poolctl setup`（仅私有/手工部署） |
|---|---|---|
| 配置位置 | `~/.mcodex/config.toml` | `~/.codex/config.toml` |
| 影响你自己的 `codex` | **不影响** | 会一起走号池 |
| 凭证 | Multica 任务内自动签发 | 需要手工永久 Key |
| multica 接入 | 设 `MULTICA_CODEX_PATH` | 无需额外设置 |
| 命令 | `mcodex` | `codex` 照旧 |

`poolctl setup` 之后不设 `GW_API_KEY` 跑 `codex`，会看到：

```
ERROR failed to refresh available models: Missing environment variable: `GW_API_KEY`
```

CHEK prod 强制使用 `mcodex`；`setup` 不能绕过生产桥接层的任务绑定校验。

`poolctl doctor` 会自动识别当前用的是哪种，并在报告里标注。

---

## 密钥管理

### 正式模式

员工侧不保存原始 `sk-clb` Key，也不把 Key 写入 Agent `custom_env`。桥接层为每个任务创建有有效期和 token 上限的原始 Key，再把它加密封装为绑定用户、Agent、任务和 Workspace 的 `mcx_` 凭证。每次 API 请求都会解封并核对三元组后才转发。

### 生命周期

```
Multica 创建任务 → daemon 注入 mat_ 任务票
  → mcodex 自动换取短时 mcx_ 凭证（不落盘）
  → 网关逐请求核对任务上下文并记账
  → 任务票完成即回收；mcx_ 与底层 Key 最晚按 TTL 失效
```

### 安全须知

- 原始网关 Key 只存在于生产桥接层和 codex-lb 之间，员工拿到的是加密、限时、任务绑定凭证。
- `mcodex` 在 daemon 配置完 shell 环境白名单后才注入凭证，Agent 的 shell 工具不会继承 `GW_API_KEY`。
- 操作系统账户所有者仍可能调试自身进程；因此本方案提供强归因、短 TTL 和审计，而不是声称能抵抗已控制员工电脑的恶意管理员。

---

## 用量与计量

### 归属依据是任务 Key

一把短时 Key 对应一个 Multica 用户和任务，名称包含用户/任务短 ID。网关按 Key 记录每次请求的模型、token 数、成本、延迟，Multica 保存同一任务的完整对话窗口。

### 采集了什么

| 采集 | 不采集 |
|---|---|
| 请求数、token 数（输入/缓存/输出/推理） | 提示词内容 |
| 模型、成本、延迟 | 代码内容 |
| 时间戳、用户/任务归属 | 提示词和回复正文的网关副本 |

### 对账粒度

**可以到人和任务。** 任务 Key 名与网关日志负责成本归集，Multica task id 负责打开对应对话窗口；网关不额外复制对话正文。

`poolctl usage --by-session` 提供会话级归集作为折中，基于同一会话的请求共享 `requestId` 前 16 位这个**实测规律**（非上游契约，可能随版本变化）。

---

## 故障排查

先跑 `poolctl doctor`，它对每个失败项都给出了下一步。以下是几个不那么直观的。

### `exceeded retry limit, last status: 429`

**这不是网络故障，是额度用完了。**

codex-lb 超限时返回 `type: "rate_limit_error"`，而 codex 只认 `usage_limit_reached`，于是走了 `RetryLimit` 分支，文案退化。

好消息：**codex 只发 1 次请求，没有重试放大**。

`poolctl doctor` 会检出这个契约不符。要根治需要让网关改用 `usage_limit_reached`。

### `does not have access to model 'xxx'`

密钥的模型白名单不含该模型。网关返回 403，而 **codex 对 403 会重试 6 次** —— 别反复手动重试，先把模型改对。

### `Invalid API key`

密钥已被吊销或输错。`poolctl status` 可以看现有密钥列表。

### 任务失败但网关显示成功

看到这个：

```
status=timeout  agent_error="codex semantic inactivity timeout after 10m0s"
failure_reason=codex_semantic_inactivity
```

但 `poolctl usage` 显示对应请求 `status=ok` —— 这是**上游延迟劣化**，不是号池故障。

实测曾在一小时内从 1 秒劣化到 12 / 25 / 46 分钟，请求全部最终成功。multica 有 10 分钟 semantic inactivity 超时，上游慢于此就会把正在正常处理的任务判定为失败。

三个后果：

1. 任务失败但**额度照扣**（网关侧请求成功，token 已计费）
2. `codex_semantic_inactivity` 在 multica 的可重试列表里，**会自动重试再扣一次**
3. 表象是"号池用不了"，极易误判

**排查**：跑 `poolctl usage` 看延迟列，中位数或最大值到分钟级就是上游问题。最慢超过 10 分钟时该命令会直接告警。

**处置**：等上游恢复、换更快的模型、或调低 `model_reasoning_effort`。号池侧无能为力。

### daemon 没走号池

```bash
poolctl daemon status
```

若显示 `! daemon 使用的 codex：/usr/bin/codex（未走号池）`，用 `poolctl daemon restart`。

常见原因是用了 `multica daemon restart` —— 它继承旧环境，新变量传不进去。

### mcodex 用错了 codex 二进制

一台机器上可能同时装了多份 codex（nvm、npm 全局、`/usr/local`），而 **daemon 的 PATH 与登录 shell 常常不同**，靠 PATH 现找会导致"你手动跑和 multica 跑用的不是同一个二进制"。

所以真 codex 路径在 `init` 时固化到 `~/.mcodex/real-codex`。记录失效时会明确报错而不是静默换一个：

```bash
poolctl mcodex init --real-codex /path/to/codex
```

固化时**不跟随软链** —— nvm / npm 的 `bin/codex` 本身就是软链，跟随会绑到底层 `codex.js`，升级即失效。

### 排查请求是否真的发出去了

网关**只在请求完成后才记账**，SSE 流式进行中查不到任何记录。别因为"网关没记录"就断定请求没发出去，看进程的 TCP 连接更准：

```bash
ss -tnp | grep <网关端口>
```

---

## 已知限制

### 对话不在网关重复落库

完整提示词、回复和工具轨迹由 Multica 原生任务窗口保存；codex-lb 仅保存请求级用量。任务 Key 名提供两边的对账键。这避免在第二套系统复制敏感正文，但也意味着网关控制台本身不能展示对话内容。

### 超限请求不进请求日志

被配额拦截的请求在网关的请求日志里查不到，所以统计不到"被拒绝了多少次"。要做超限告警不能依赖请求日志。

### 多账号 sticky 路由未验证

实测环境只有一个账号。README 里 93.9% 的缓存命中率是**单账号结果**。

多账号池化后，如果会话没有粘到同一个账号，提示词缓存会失效，成本优势会显著缩水。**加第二个账号后必须重测。**

### 本机所有者边界

凭证不会进入 Agent shell，但掌控员工操作系统账户的人仍可能检查 Codex 进程。短 TTL、任务绑定和审计用于缩小风险窗口，不能替代终端安全管理。

### 版本绑定

本项目的结论绑定具体版本（codex-cli 0.147.0 / multica 0.3.17）。`wire_api` 取值、429 契约、重试参数都可能随版本变化。**建议 pin 住 codex 版本，升级前重跑 `poolctl doctor`。**

---

## 实测数据

环境：codex-cli 0.147.0 / multica 0.3.17 / codex-lb / Codex Pro 单账号。完整记录见 [`docs/实测记录.md`](docs/实测记录.md)。

### 隔离性

| 命令 | 启动横幅 | 网关日志 |
|---|---|---|
| `codex` | `provider: openai` | 0 条新增 |
| `mcodex` | `provider: gw` | +1 条，按密钥正确归属 |

### 提示词缓存不丢

一次 multica 任务的连续三轮：

| 轮次 | 输入 tokens | 其中缓存 | 命中率 |
|---|---|---|---|
| 1 | 19,838 | 0 | 0% |
| 2 | 20,926 | 19,200 | 91.8% |
| 3 | 21,550 | 20,224 | 93.9% |

**这是方案经济性的前提** —— 同类中转方案存在缓存丢失导致额度成倍消耗的情况。

### 安全承诺

| 承诺 | 实测 |
|---|---|
| 吊销立即生效 | 0.06 秒返回 401 |
| 模型白名单 | 403 `model_not_allowed`，有效 |
| 周额度上限 | 429，有效 |
| 密钥明文只返回一次 | 后续查询只有前缀 |

---

## 设计取舍

### 为什么用文本 marker 块而不是 TOML 库改写配置

1. multica daemon 会把宿主 `config.toml` **原样拷贝**进每个 per-task `CODEX_HOME`，保持纯文本可预测地被拷贝，比依赖某个 TOML 库的序列化风格更安全。
2. 保留用户原有的注释与键顺序 —— TOML 库重写整个文件会打乱既有内容。
3. 与 multica 自身做法一致（它也用 `# BEGIN multica-managed` 文本块）。

**TOML 顶层键陷阱**：`model_provider = "gw"` 是顶层键，必须出现在任何 `[table]` 之前，否则会被解析成上一个 table 的成员。所以注入点固定为"第一个 table 头之前"。写入后一律用 `tomllib` 重新解析校验，语义不符则回滚。测试里有专门一组用例守着这条。

### 为什么固化 codex 路径而不是每次现找

见[故障排查](#mcodex-用错了-codex-二进制)。这在实测中真实发生过。

### 为什么用 `execvp` 而不是 `subprocess`

daemon 用 `app-server --listen stdio://` 通过 stdio 与 codex 通信，参数受 `codexBlockedArgs` 硬校验。任何包装、缓冲或参数改写都会让链路失效。`execvp` 直接替换进程映像，argv 与 stdio 完全继承。

### 为什么不把 token 数回灌进 multica

multica 的用量写入是 REPLACE 语义，会与 agent 自报互相覆盖；而且两边的 `input_tokens` 桶定义相反（multica 侧已减去 cached，网关侧是含 cached 的原始值），直接相加必然差一个量级。

正确做法是只回填权威成本字段，不动 token 数。

---

## 开发

```bash
pip install -e ".[dev]"
pytest              # 71 个测试
```

测试重点覆盖：

- TOML 注入的语义正确性与幂等性（顶层键陷阱、marker 块共存、反复注入稳定）
- 与用户 `~/.codex` 的隔离性（不改源文件、不复制凭据）
- codex 路径解析优先级（固化 > 环境变量 > PATH，软链不跟随）
- daemon 场景下不覆盖 `CODEX_HOME`，并强制用任务票换取临时凭证
- doctor 的模式识别

### 项目结构

```
corp_codex_pool/
├── codex_config.py   # provider 注入（幂等 marker 块 + tomllib 校验）
├── mcodex.py         # mcodex 入口，execvp 交棒
├── gateway.py        # codex-lb 客户端，用量归集
├── multica.py        # multica 客户端，custom_env 下发
├── doctor.py         # 体检
├── config.py         # 配置加载
└── cli.py            # poolctl 命令行
docs/
├── 设计方案.md        # 源码级设计推导，断言带 path:line
├── 实测记录.md        # 实测结论，尤其是与预期不一致的部分
└── 员工须知.md        # 可直接发给员工
```

---

## FAQ

**Q: 会影响我原来的 `codex` 吗？**

不会。`mcodex` 用独立的家目录，两者可并存。只有 `poolctl setup` 那种方式才会影响。

**Q: 员工需要 Codex 账号吗？**

不需要，也不需要 `codex login`。有 `env_key` 时 codex 不走 first-party auth 路径。

**Q: 员工需要申请或保存密钥吗？**

不需要。`mcodex` 在每个 Multica 任务启动时自动申请，凭证不写磁盘；失败时重新创建/重试任务即可。

**Q: 能限制员工只用某几个模型吗？**

能，`--allowed-model`。注意白名单外的模型会触发 codex 重试 6 次。

**Q: 支持飞连 / OIDC 登录吗？**

CHEK prod 已通过 `multica.chekkk.com` 前置飞连 OIDC 单点登录；任务票据仍由 Multica 服务端签发。

**Q: 支持自托管 multica 吗？**

支持，改 `MULTICA_SERVER_URL` 即可。本项目对云端和自托管没有区别对待。

**Q: 以后功能更新在哪个仓库做？**

员工端在 `chekdata/corp-codex-pool`，任务身份契约在 `chekdata/multica`，网关能力在 `chekdata/codex-lb`，生产策略和 ArgoCD 发布在 `chekdata/ops-bootstrap`。

**Q: 为什么我的请求要几分钟？**

多半是上游延迟劣化。跑 `poolctl usage` 看延迟列。详见[故障排查](#任务失败但网关显示成功)。

**Q: 这个能用于转售订阅额度吗？**

不能，也不该。本项目是给企业内部分发自购订阅用的，请遵守你所购买服务的条款。

---

## 许可

MIT
