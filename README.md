# corp-codex-pool

把多个 Codex Pro 订阅变成一个内部号池，按人发密钥、按人计量，并让 [multica](https://github.com/multica-ai/multica) 拉起的 codex 自动走这个号池。

**不改 multica 一行代码，不改 codex 一行代码。**

---

## 它解决什么

号池网关（codex-lb）知道"这次调用花了多少"，但不知道"是谁调的"。multica 知道人、任务、工作区，但它明确拒绝自己做 LLM 代理。两边各跑各的，结果是网关有量无人、multica 有人无量。

这个工具把两边接上：

| | 只有网关 | 只有 multica | 接上之后 |
|---|---|---|---|
| 多个订阅共享 | 有 | 无 | 有 |
| 按人计量 | 做不到（只看得见密钥） | 无请求级用量 | 密钥即人，可归集 |
| 超额拦截 | 可做 | 物理上做不到 | 网关拦截 |
| 员工无需登录 | — | — | 密钥由服务端下发 |

## 设计前提：不碰你的 codex

你的 `codex` 和 `~/.codex/` 保持原样。号池走的是一个独立命令 **`mcodex`**，它有自己的家目录 `~/.mcodex`。两者并存：

```
codex   →  provider: openai  →  你的个人 ChatGPT 订阅
mcodex  →  provider: gw      →  公司号池网关  →  Pro 账号 A/B/N
```

同一台机器、同一个模型，走不同上游，互不影响。

## 原理

`mcodex` 是个薄封装：准备好环境后用 `execvp` 交棒给真 codex，argv 与 stdio 原样透传。它覆盖两种场景：

**你直接用** — 设 `CODEX_HOME=~/.mcodex`（那里有号池 provider 配置和密钥），然后 exec codex。

**multica daemon 拉起** — daemon 会把 `CODEX_HOME` 指向 per-task 目录并从 `~/.codex/config.toml` 拷一份配置进去。既然宿主那份不含号池 provider，`mcodex` 就在 exec 前把 provider 注入这份 per-task 配置。daemon 准备完就不再动它，所以这个时机是安全的，且每个任务都是新目录。

```
                    你 ──────────────────────► mcodex ──► CODEX_HOME=~/.mcodex
                                                  │
multica ──► daemon ──► MULTICA_CODEX_PATH=mcodex ─┘        （注入 per-task 配置）
                                                  │
                                                  ▼  execvp（argv/stdio 原样透传）
                                          codex app-server
                                                  │  POST /v1/responses
                                                  ▼
                                          codex-lb 号池网关
                                    ┌─────────────┼─────────────┐
                                    ▼             ▼             ▼
                               Pro 账号 A    Pro 账号 B    Pro 账号 N
```

背后依赖三个既有机制，全是上游本来的行为，没有一处 hack：

1. **codex 自定义 provider 的 `env_key` 从环境变量取密钥，且优先于 `auth.json`**，有 `env_key` 时不需要 `codex login` → 每人一把密钥，不用分发账号。
2. **multica daemon 支持 `MULTICA_CODEX_PATH` 指定 codex 可执行文件路径** → 不改 multica 就能换成 `mcodex`。
3. **multica 的 `custom_env` 会被 daemon 铺到 codex 子进程环境**，且不拦自定义变量名 → 密钥可以按人下发到各自的 agent。

完整的源码级论证见 [`docs/设计方案.md`](docs/设计方案.md)，所有断言都带 `path:line`。

## 安装

```bash
git clone <this repo> && cd corp-codex-pool
pip install -e .
cp .env.example .env   # 填写网关地址与管理面密码
```

需要 Python 3.11+（用到标准库 `tomllib`）。

## 用法

### 员工本机

```bash
# 签发密钥（在管理机上做），然后在员工机上初始化 mcodex
poolctl issue zhangsan --weekly-token-limit 20000000
echo "<密钥>" | poolctl mcodex init --key-stdin

# 之后照常用，只是命令名换成 mcodex
mcodex exec "重构这个模块"
mcodex
```

`mcodex init` 默认从 `~/.codex/config.toml` 继承使用偏好（模型、推理强度、信任目录），所以手感和平时一致。**只继承偏好，不复制任何凭据。**

### 让 multica 走号池

给 daemon 设一个环境变量即可，不改 multica：

```bash
export MULTICA_CODEX_PATH="$(poolctl mcodex path)"
multica daemon restart
```

daemon 之后拉起的就是 `mcodex`。要按人发不同密钥（一台机器多个 agent），再把密钥写进各自 agent 的 `custom_env`：

```bash
poolctl issue zhangsan --agent-id <agent-uuid>
```

`custom_env` 里的密钥优先于 `~/.mcodex/key`，所以两种模式可以混用。

### 运维

```bash
poolctl doctor --key sk-clb-...   # 链路体检
poolctl usage                     # 按人看用量
poolctl revoke zhangsan --agent-id <agent-uuid>   # 吊销，网关侧立即生效
poolctl status                    # 号池概况
```

### 不用 mcodex 的另一种做法

`poolctl setup` 会把号池 provider 直接注入宿主 `~/.codex/config.toml`（幂等、自动备份、`poolctl unsetup` 可完整回滚）。这样连 `MULTICA_CODEX_PATH` 都不用设，但**你自己的 `codex` 也会走号池**，且未设 `GW_API_KEY` 时会直接报错。

除非你确实想让整台机器都走号池，否则用 `mcodex`。

`poolctl usage` 输出：

```
密钥                              请求          输入          缓存        输出      命中率      成本USD
----------------------------------------------------------------------------------------
pool-zhangsan                   22     254,863     126,208       568    49.5%     0.6513
pool-lisi                       14      75,818           0       127     0.0%     0.0766
```

所有会改状态的命令都支持 `--dry-run`。`setup` 会自动备份原配置，`unsetup` 可完整回滚。

## 已验证

在真实环境端到端跑通（codex-cli 0.147.0 / multica 0.3.17 / codex-lb）：

**隔离性** — 同一台机器上做对照，同一个模型：

| 命令 | 启动横幅 | 网关日志 |
|---|---|---|
| `codex` | `provider: openai` | 0 条新增 |
| `mcodex` | `provider: gw` | +1 条，按密钥正确归属 |

**multica 链路** — `MULTICA_CODEX_PATH` 指向 mcodex 后派真实任务：

- daemon 日志确认 `exec=/…/mcodex args="[app-server --listen stdio://]"`，参数完整透传
- mcodex 成功把 provider 注入 per-task `codex-home/config.toml`，与 multica 自己的托管块共存不冲突
- codex 子进程同时拿到 `GW_API_KEY`、`CODEX_HOME`、`MULTICA_TASK_ID`/`AGENT_ID`/`WORKSPACE_ID`
- 任务正常完成，请求以 `ua=multica-agent-sdk` 出现在网关日志
- 连续三轮的缓存命中率 0% → 91.8% → 93.9%，**证明经号池中转不丢提示词缓存**——这是方案经济性的前提

`poolctl doctor` 12 项通过 0 失败，58 个单测全过。

## 几件必须知道的事

**网关必须实现 Responses API。** codex 已经移除了 `wire_api = "chat"`，只接受 `"responses"`。只做 chat/completions 兼容的网关接不上。

**超限必须返回 429 + `usage_limit_reached`。** codex 有双层重试，拒绝形式直接决定请求放大倍数：

| 拒绝形式 | 实际请求数 |
|---|---|
| 429 + `{"error":{"type":"usage_limit_reached"}}` | 1 |
| 402 / 403 / 401 / 404 | 6 |
| 5xx | 最多 30 |

用 5xx 表达配额等于自己打爆自己。

**密钥对 agent 的 shell 可见。** `custom_env` 里的键会进 codex 的 `shell_environment_policy.include_only`，agent 自己跑 `env` 能看到号池密钥。号池密钥只是入场券——泄露后果限于该员工额度，且可即时吊销。若安全评审不接受，需要改 multica 上游改走 daemon 侧 `agentEnv`（含 `KEY` 的变量会被排除出 `include_only`，但主进程仍拿得到）。

**号池 agent 一律 private。** multica 的 `agent_runtime.visibility` 若为 `public`，工作区任何成员派的活都跑在这个 owner 的号上，"一人一密钥"会退化成"一机一密钥"。

**用量真相源在网关。** multica 侧的 codex 用量实际是扫 rollout 文件得来的，丢 `cache_write`、一个任务只有一行、且只在任务结束时上报一次。不要把 token 数回灌进 multica，只回填成本。

**pin 住 codex 版本。** 上面这些结论都绑定具体版本，升级前应重跑 `poolctl doctor`。

## 开发

```bash
pytest          # 29 个测试，重点覆盖 TOML 注入的语义正确性与幂等性
```

注入逻辑用文本 marker 块而非 TOML 库改写，原因见 `corp_codex_pool/codex_config.py` 顶部注释。最容易出错的地方是 TOML 顶层键必须落在任何 `[table]` 之前，测试里有专门一组用例守着。

## 许可

MIT
