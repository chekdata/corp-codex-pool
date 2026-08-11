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

## 原理

三个已验证的机制串起来，全是既有行为，没有一处 hack：

1. **multica daemon 把宿主 `~/.codex/config.toml` 原样拷贝进每个 per-task `CODEX_HOME`**，并且只往拷贝里 upsert 自己的托管块，不碰 `[model_providers.*]`。
   → 所以把号池 provider 写进宿主配置，就会自动流到每个任务。

2. **codex 的自定义 provider 用 `env_key` 从环境变量取密钥，且优先于 `auth.json`**，有 `env_key` 时不需要 `codex login`。
   → 所以每人一把密钥，不用分发账号。

3. **multica 的 `custom_env` 会被 daemon 铺到 codex 子进程环境**，且不拦自定义变量名。
   → 所以密钥可以按人下发到各自的 agent。

```
宿主 ~/.codex/config.toml          multica agent custom_env
   [model_providers.gw]                  GW_API_KEY=sk-clb-…
   base_url = 号池网关                        │
        │                                    │
        └──── daemon 拷贝 ────┐    ┌── daemon 注入 ──┘
                             ▼    ▼
                    per-task CODEX_HOME + env
                             │
                             ▼
                    codex app-server
                             │  POST /v1/responses
                             ▼
                      codex-lb 号池网关
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Pro 账号 A     Pro 账号 B     Pro 账号 N
```

完整的源码级论证见 [`docs/设计方案.md`](docs/设计方案.md)，所有断言都带 `path:line`。

## 安装

```bash
git clone <this repo> && cd corp-codex-pool
pip install -e .
cp .env.example .env   # 填写网关地址与管理面密码
```

需要 Python 3.11+（用到标准库 `tomllib`）。

## 用法

```bash
# 1. 把号池 provider 注入宿主 codex 配置（幂等，自动备份）
poolctl setup --dry-run    # 先看要改什么
poolctl setup

# 2. 为员工签发密钥，并下发到他的 multica agent
poolctl issue zhangsan --agent-id <agent-uuid> --weekly-token-limit 20000000

# 3. 体检
poolctl doctor --key sk-clb-...

# 4. 按人看用量
poolctl usage

# 5. 离职/泄露时吊销，网关侧立即生效
poolctl revoke zhangsan --agent-id <agent-uuid>
```

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

- codex 启动横幅显示 `provider: gw`，请求出现在网关日志，按密钥正确归属
- multica daemon 拉起的 codex，其 per-task `codex-home/config.toml` 完整继承号池块，TOML 语义正确，与 multica 自己的托管块共存不冲突
- codex 子进程环境同时拿到 `GW_API_KEY`、`CODEX_HOME`、`MULTICA_TASK_ID`/`AGENT_ID`/`WORKSPACE_ID`
- `poolctl doctor` 10 项全通过

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
