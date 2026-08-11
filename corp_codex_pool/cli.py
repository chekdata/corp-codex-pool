"""poolctl —— Codex 号池与 multica 运行时的集成层命令行。

设计原则：
- 所有会改变状态的命令都支持 --dry-run，且默认打印将要发生的变更。
- 密钥明文只在签发那一刻出现在终端一次，其余场合一律打码。
- 每一步失败都给出可执行的下一步，而不是只报错。
"""

from __future__ import annotations

import json as jsonlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from . import doctor as doctor_mod
from .codex_config import (
    ConfigInjectionError,
    ProviderSpec,
    default_config_path,
    inject,
    remove,
)
from .config import Settings
from .gateway import (
    AuthRequired,
    GatewayClient,
    GatewayError,
    summarize_by_key,
    summarize_by_session,
)
from .multica import MulticaClient, MulticaError

CONTEXT = {"help_option_names": ["-h", "--help"]}


def _settings(ctx) -> Settings:
    return ctx.obj["settings"]


def _fail(message: str, hint: str = "") -> None:
    click.secho(f"✗ {message}", fg="red", err=True)
    if hint:
        click.echo(f"  → {hint}", err=True)
    sys.exit(1)


@click.group(context_settings=CONTEXT)
@click.option("--env-file", type=click.Path(path_type=Path), help="指定 .env 路径")
@click.pass_context
def main(ctx, env_file):
    """Codex 号池 × multica 集成层。"""
    ctx.ensure_object(dict)
    ctx.obj["settings"] = Settings.load(env_file)


# ---------------------------------------------------------------- setup

@main.command()
@click.option("--base-url", help="网关地址，覆盖 POOL_BASE_URL")
@click.option("--provider-id", help="provider id，覆盖 POOL_PROVIDER_ID")
@click.option("--env-key", help="密钥环境变量名，覆盖 POOL_ENV_KEY")
@click.option("--config", "config_path", type=click.Path(path_type=Path), help="codex config.toml 路径")
@click.option("--no-default", is_flag=True, help="只注册 provider，不设为默认")
@click.option("--no-headers", is_flag=True, help="不注入对账请求头")
@click.option("--dry-run", is_flag=True, help="只显示将要写入的内容")
@click.pass_context
def setup(ctx, base_url, provider_id, env_key, config_path, no_default, no_headers, dry_run):
    """向宿主 ~/.codex/config.toml 注入号池 provider。

    multica daemon 会把这个文件原样拷贝进每个 per-task CODEX_HOME，
    因此这一步做完，daemon 拉起的 codex 就会走号池，无需改 multica。
    """
    settings = _settings(ctx)
    spec = ProviderSpec(
        provider_id=provider_id or settings.provider_id,
        base_url=base_url or settings.pool_base_url,
        env_key=env_key or settings.env_key,
        set_default=not no_default,
    )
    if no_headers:
        spec.env_http_headers = {}

    path = config_path or default_config_path()

    try:
        result = inject(spec, path=path, dry_run=dry_run)
    except ConfigInjectionError as exc:
        _fail(str(exc))
        return

    if not result.changed:
        click.secho(f"✓ 配置已是目标状态，无需改动：{result.path}", fg="green")
        return

    if dry_run:
        click.secho(f"[dry-run] 将写入 {result.path}：", fg="yellow")
        _print_block_diff(result.before, result.after)
        return

    click.secho(f"✓ 已注入 {result.path}", fg="green")
    if result.backup:
        click.echo(f"  备份：{result.backup}")
    click.echo(f"  provider = {spec.provider_id}   base_url = {spec.base_url}")
    click.echo(f"  密钥来源环境变量：{spec.env_key}")
    click.echo("\n下一步：poolctl issue <员工标识> 签发密钥并下发")


def _print_block_diff(before: str, after: str) -> None:
    import difflib

    diff = difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), "当前", "注入后", n=2
    )
    for line in diff:
        color = "green" if line.startswith("+") else "red" if line.startswith("-") else None
        click.secho(line.rstrip("\n"), fg=color)


@main.command("unsetup")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True)
def unsetup(config_path, dry_run):
    """移除注入的 provider 块，恢复注入前状态。"""
    result = remove(path=config_path or default_config_path(), dry_run=dry_run)
    if not result.changed:
        click.echo("未找到托管块，无需改动")
        return
    click.secho(
        f"{'[dry-run] 将移除' if dry_run else '✓ 已移除'}托管块：{result.path}",
        fg="yellow" if dry_run else "green",
    )


# ---------------------------------------------------------------- issue

@main.command()
@click.argument("person")
@click.option("--agent-id", help="下发到该 multica agent 的 custom_env")
@click.option("--workspace-id", help="multica workspace id，默认取本机 CLI 配置")
@click.option("--weekly-token-limit", type=int, help="周令牌上限")
@click.option("--allowed-model", multiple=True, help="模型白名单，可重复")
@click.option("--expires-days", type=int, help="有效期天数")
@click.option("--traffic-class", type=click.Choice(["foreground", "opportunistic"]), help="流量等级")
@click.option("--reuse/--no-reuse", default=True, help="同名密钥已存在时复用而非报错")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def issue(
    ctx, person, agent_id, workspace_id, weekly_token_limit, allowed_model,
    expires_days, traffic_class, reuse, dry_run,
):
    """为一名员工签发号池密钥，可选直接下发到 multica agent。

    PERSON 是员工标识，会成为密钥名（建议用工号或邮箱前缀）。
    """
    settings = _settings(ctx)
    key_name = f"pool-{person}"

    try:
        with GatewayClient(settings.pool_admin_url, settings.pool_admin_password) as gw:
            gw.login()
            existing = gw.find_key(key_name)

            if existing and not reuse:
                _fail(f"密钥 {key_name} 已存在", "加 --no-reuse 之外的选项，或换个 PERSON")

            if existing:
                click.secho(f"! 密钥 {key_name} 已存在，复用（明文不可再取）", fg="yellow")
                click.echo(f"  id={existing['id']}  prefix={existing.get('keyPrefix')}")
                secret = None
                key_id = existing["id"]
            elif dry_run:
                click.secho(f"[dry-run] 将签发密钥 {key_name}", fg="yellow")
                if weekly_token_limit:
                    click.echo(f"  周令牌上限：{weekly_token_limit:,}")
                if allowed_model:
                    click.echo(f"  模型白名单：{', '.join(allowed_model)}")
                secret, key_id = None, "<dry-run>"
            else:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(days=expires_days)
                    if expires_days
                    else None
                )
                issued = gw.create_key(
                    key_name,
                    weekly_token_limit=weekly_token_limit,
                    allowed_models=list(allowed_model) or None,
                    expires_at=expires_at,
                    traffic_class=traffic_class,
                )
                secret, key_id = issued.secret, issued.id
                click.secho(f"✓ 已签发 {key_name}", fg="green")
                click.echo(f"  id={key_id}  prefix={issued.key_prefix}")
                click.secho(f"\n  密钥明文（只显示这一次）：{secret}\n", fg="cyan", bold=True)

                if allowed_model:
                    # 实测：命中白名单外的模型，网关返回 403 model_not_allowed，
                    # 而 codex 对 403 会重试 6 次。请求都被拦在网关、不消耗上游额度，
                    # 但会放大网关负载与错误日志，所以白名单要和员工实际用的模型对齐。
                    click.secho(
                        "  提示：白名单外的模型会被网关 403 拒绝，而 codex 对 403 会重试 6 次。\n"
                        "  请确认白名单覆盖了员工日常使用的模型，避免无谓的重试放大。",
                        fg="yellow",
                    )
    except AuthRequired as exc:
        _fail(str(exc), "在 .env 设置 POOL_ADMIN_PASSWORD")
        return
    except GatewayError as exc:
        _fail(f"网关操作失败：{exc}")
        return

    if not agent_id:
        click.echo("未指定 --agent-id，跳过下发。可稍后运行：")
        click.echo(f"  poolctl deliver --agent-id <id> --key <明文>")
        return

    if not secret:
        _fail(
            "复用已有密钥时拿不到明文，无法自动下发",
            "先 poolctl revoke 再重新 issue，或用 poolctl deliver --key <明文> 手工下发",
        )
        return

    _deliver(settings, agent_id, workspace_id, secret, dry_run)


# ---------------------------------------------------------------- deliver

@main.command()
@click.option("--agent-id", required=True, help="目标 multica agent")
@click.option("--workspace-id", help="multica workspace id")
@click.option("--key", required=True, help="密钥明文")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def deliver(ctx, agent_id, workspace_id, key, dry_run):
    """把密钥写入指定 multica agent 的 custom_env。"""
    _deliver(_settings(ctx), agent_id, workspace_id, key, dry_run)


def _deliver(settings: Settings, agent_id: str, workspace_id: str | None, secret: str, dry_run: bool) -> None:
    try:
        with MulticaClient.from_local_config(workspace_id=workspace_id) as mc:
            result = mc.set_env(agent_id, {settings.env_key: secret}, dry_run=dry_run)
    except MulticaError as exc:
        _fail(f"multica 操作失败：{exc}", "确认已 multica login，且 agent-id / workspace-id 正确")
        return

    if not result["changed"]:
        click.secho(f"✓ {settings.env_key} 已是目标值，无需改动", fg="green")
        return

    prefix = "[dry-run] 将写入" if dry_run else "✓ 已写入"
    click.secho(f"{prefix} agent {agent_id} 的 custom_env", fg="yellow" if dry_run else "green")
    click.echo(f"  变更前：{result['before'] or '{}'}")
    click.echo(f"  变更后：{result['after']}")
    if not dry_run:
        click.echo("\n  注意：custom_env 里的键对 agent 自己的 shell 可见。")
        click.echo("  号池密钥仅为入场券，可随时吊销。")


# ---------------------------------------------------------------- revoke

@main.command()
@click.argument("person")
@click.option("--agent-id", help="同时清除该 agent 的 custom_env")
@click.option("--workspace-id")
@click.option("--yes", is_flag=True, help="跳过确认")
@click.pass_context
def revoke(ctx, person, agent_id, workspace_id, yes):
    """吊销某员工的号池密钥。网关侧立即生效。"""
    settings = _settings(ctx)
    key_name = f"pool-{person}"

    try:
        with GatewayClient(settings.pool_admin_url, settings.pool_admin_password) as gw:
            gw.login()
            existing = gw.find_key(key_name)
            if not existing:
                _fail(f"找不到密钥 {key_name}")
                return

            if not yes:
                click.echo(f"将吊销：{key_name}  id={existing['id']}  prefix={existing.get('keyPrefix')}")
                click.confirm("确认吊销？", abort=True)

            gw.revoke_key(existing["id"])
            click.secho(f"✓ 已吊销 {key_name}", fg="green")
    except GatewayError as exc:
        _fail(f"吊销失败：{exc}")
        return

    if agent_id:
        try:
            with MulticaClient.from_local_config(workspace_id=workspace_id) as mc:
                result = mc.unset_env(agent_id, [settings.env_key])
            if result["changed"]:
                click.secho(f"✓ 已从 agent {agent_id} 清除 {settings.env_key}", fg="green")
        except MulticaError as exc:
            click.secho(f"! 网关密钥已吊销，但清除 agent 环境变量失败：{exc}", fg="yellow")


# ---------------------------------------------------------------- usage

@main.command()
@click.option("--since", help="起始时间，ISO8601")
@click.option("--until", help="结束时间，ISO8601")
@click.option("--max-records", type=int, default=5000, show_default=True)
@click.option("--by-session", is_flag=True, help="按会话归集，看单次任务花了多少")
@click.option("--json", "as_json", is_flag=True, help="输出 JSON")
@click.pass_context
def usage(ctx, since, until, max_records, by_session, as_json):
    """按人汇总用量。归属依据是密钥，密钥由 poolctl issue 与员工绑定。"""
    settings = _settings(ctx)
    try:
        with GatewayClient(settings.pool_admin_url, settings.pool_admin_password) as gw:
            gw.login()
            rows = gw.request_logs(since=since, until=until, max_records=max_records)
    except GatewayError as exc:
        _fail(f"拉取用量失败：{exc}")
        return

    if by_session:
        _print_sessions(rows, as_json)
        return

    summary = summarize_by_key(rows)

    if as_json:
        click.echo(jsonlib.dumps(summary, ensure_ascii=False, indent=2))
        return

    if not summary:
        click.echo("时间范围内没有请求记录")
        return

    click.echo(f"共 {len(rows)} 条请求，按密钥归集：\n")
    header = (
        f"{'密钥':<26}{'请求':>6}{'输入':>12}{'缓存':>12}{'命中率':>9}"
        f"{'延迟中位':>10}{'最慢':>10}{'成本USD':>11}"
    )
    click.echo(header)
    click.echo("-" * len(header))

    def fmt_latency(ms):
        if ms is None:
            return "—"
        return f"{ms / 1000:.1f}s" if ms < 60_000 else f"{ms / 60_000:.1f}m"

    for bucket in sorted(summary.values(), key=lambda b: -b["costUsd"]):
        name = bucket["apiKeyName"] or "(未归属)"
        rate = f"{bucket['cacheHitRate']:.1%}" if bucket["cacheHitRate"] is not None else "—"
        click.echo(
            f"{name:<26}{bucket['requests']:>6}{bucket['inputTokens']:>12,}"
            f"{bucket['cachedInputTokens']:>12,}{rate:>9}"
            f"{fmt_latency(bucket['latencyP50Ms']):>10}"
            f"{fmt_latency(bucket['latencyMaxMs']):>10}{bucket['costUsd']:>11.4f}"
        )

    total = sum(b["costUsd"] for b in summary.values())
    click.echo("-" * len(header))
    click.echo(
        f"{'合计':<26}{len(rows):>6}{'':>12}{'':>12}{'':>9}{'':>10}{'':>10}{total:>11.4f}"
    )

    # 上游劣化时单次请求可能超过 multica 的 10 分钟 semantic inactivity timeout，
    # 任务会被杀掉而请求其实是成功的。把这条提到明面上，免得被当成号池故障。
    slowest = max(
        (b["latencyMaxMs"] or 0 for b in summary.values()),
        default=0,
    )
    if slowest >= 600_000:
        click.secho(
            f"\n! 最慢一次请求耗时 {slowest / 60_000:.1f} 分钟，已超过 multica 的 "
            "10 分钟无活动超时。\n"
            "  上游劣化期间 multica 任务会被判定为 codex_semantic_inactivity 而失败，"
            "但网关侧请求其实是成功的（额度照扣）。\n"
            "  该失败原因在 multica 的可重试列表里，会自动重试并再次消耗额度。",
            fg="yellow",
        )


def _print_sessions(rows, as_json: bool) -> None:
    sessions = summarize_by_session(rows)

    if as_json:
        click.echo(jsonlib.dumps(sessions, ensure_ascii=False, indent=2))
        return

    if not sessions:
        click.echo("没有可归集的会话")
        return

    click.echo(f"共 {len(sessions)} 个会话：\n")
    header = f"{'开始时间':<20}{'来源':<8}{'密钥':<20}{'请求':>5}{'输入':>11}{'命中率':>8}{'成本USD':>10}"
    click.echo(header)
    click.echo("-" * len(header))

    for item in sessions[:40]:
        rate = f"{item['cacheHitRate']:.1%}" if item["cacheHitRate"] is not None else "—"
        source = "multica" if item["sourceIsApp"] else "手动"
        click.echo(
            f"{(item['firstAt'] or '')[:19]:<20}{source:<8}"
            f"{(item['apiKeyName'] or '(未归属)'):<20}{item['requests']:>5}"
            f"{item['inputTokens']:>11,}{rate:>8}{item['costUsd']:>10.4f}"
        )

    if len(sessions) > 40:
        click.echo(f"… 另有 {len(sessions) - 40} 个会话未显示（用 --json 获取全部）")

    click.echo(
        "\n注：会话标识优先取网关的 conversationId；app-server 模式下该字段为空，"
        "退回按 requestId 前缀分组（实测规律，非上游契约）。"
    )


# ---------------------------------------------------------------- doctor

@main.command()
@click.option("--key", help="用该密钥做端到端探测")
@click.option("--config", "config_path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def doctor(ctx, key, config_path, as_json):
    """体检：逐项验证号池链路是否真的成立。"""
    settings = _settings(ctx)
    checks = [
        doctor_mod.check_gateway_reachable(settings.pool_base_url),
        doctor_mod.check_models_endpoint(settings.pool_base_url),
        *doctor_mod.check_codex_config(
            settings.provider_id, settings.pool_base_url, settings.env_key, config_path
        ),
        *doctor_mod.check_mcodex_home(settings.env_key),
    ]

    if key:
        checks.append(doctor_mod.check_key_works(settings.pool_base_url, key))
        checks.append(doctor_mod.check_rate_limit_headers(settings.pool_base_url, key))
        checks.append(doctor_mod.check_quota_contract(settings.pool_base_url, key))

    try:
        with MulticaClient.from_local_config() as mc:
            identity = mc.me()
            checks.append(
                doctor_mod.Check("multica 连通", doctor_mod.OK, f"{identity.name} <{identity.email}>")
            )
            agents = mc.agents()
            delivered = [
                a for a in agents if settings.env_key in (mc.get_env(a["id"]) or {})
            ]

            # 密钥有两个来源，任一成立即可：
            #   per-agent  —— agent 的 custom_env，适合一台机器多人/多档位
            #   机器级     —— mcodex 家目录的密钥文件，一台机器一把
            # 只有两者都没有才是真问题。
            from .mcodex import KEY_FILENAME, mcodex_home

            machine_key = (mcodex_home() / KEY_FILENAME).exists()

            if delivered:
                checks.append(
                    doctor_mod.Check(
                        "agent 密钥来源",
                        doctor_mod.OK,
                        f"{len(delivered)}/{len(agents)} 个 agent 带有 {settings.env_key}"
                        "（per-agent，优先于机器级密钥）",
                    )
                )
            elif machine_key:
                checks.append(
                    doctor_mod.Check(
                        "agent 密钥来源",
                        doctor_mod.OK,
                        f"未使用 custom_env，{len(agents)} 个 agent 统一走 mcodex 机器级密钥",
                    )
                )
            else:
                checks.append(
                    doctor_mod.Check(
                        "agent 密钥来源",
                        doctor_mod.FAIL,
                        f"{len(agents)} 个 agent 都没有 {settings.env_key}，"
                        "mcodex 家目录也没有密钥文件",
                        "机器级：poolctl mcodex init --key-stdin\n"
                        "    或按人下发：poolctl issue <员工> --agent-id <id>",
                    )
                )
    except MulticaError as exc:
        checks.append(
            doctor_mod.Check("multica 连通", doctor_mod.WARN, str(exc), "运行 multica login")
        )

    if as_json:
        click.echo(
            jsonlib.dumps(
                [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    report, passed = doctor_mod.render(checks)
    click.echo(report)
    sys.exit(0 if passed else 1)


# ---------------------------------------------------------------- mcodex

@main.group()
def mcodex():
    """管理 mcodex —— 走号池的独立 codex 封装。

    mcodex 使用自己的家目录（默认 ~/.mcodex），不碰用户的 ~/.codex。
    `codex` 走个人订阅，`mcodex` 走公司号池，两者并存。
    """


@mcodex.command("init")
@click.option("--key", help="号池密钥明文，存入家目录（600）")
@click.option("--key-stdin", is_flag=True, help="从 stdin 读密钥，避免出现在 shell 历史")
@click.option("--inherit/--no-inherit", default=True, show_default=True,
              help="从 ~/.codex/config.toml 继承使用偏好（模型、信任目录），不含任何凭据")
@click.option("--home", type=click.Path(path_type=Path), help="自定义家目录")
@click.option("--real-codex", type=click.Path(path_type=Path),
              help="真 codex 的绝对路径。不给则自动探测并固化，"
                   "避免 daemon 与登录 shell 因 PATH 不同选到不同二进制")
@click.pass_context
def mcodex_init(ctx, key, key_stdin, inherit, home, real_codex):
    """初始化 mcodex 家目录。"""
    from .mcodex import DEFAULT_MCODEX_HOME, McodexError, init_home

    settings = _settings(ctx)

    if key_stdin:
        key = sys.stdin.read().strip()

    spec = ProviderSpec(
        provider_id=settings.provider_id,
        base_url=settings.pool_base_url,
        env_key=settings.env_key,
    )

    target = home or DEFAULT_MCODEX_HOME
    try:
        info = init_home(
            target,
            spec,
            inherit_from=Path.home() / ".codex" / "config.toml" if inherit else None,
            key=key,
            real_codex=str(real_codex) if real_codex else None,
        )
    except McodexError as exc:
        _fail(str(exc))
        return

    click.secho(f"✓ mcodex 家目录就绪：{info['home']}", fg="green")
    click.echo(f"  配置：{info['config']}")
    click.echo(f"  provider = {spec.provider_id}   base_url = {spec.base_url}")
    if info["real_codex"]:
        click.echo(f"  真 codex：{info['real_codex']}（已固化）")
    else:
        click.secho("  ! 未探测到 codex，请用 --real-codex 指定", fg="yellow")
    if info["inherited"]:
        click.echo(f"  已继承偏好：{', '.join(info['inherited'])}（未复制任何凭据）")
    if info["key_stored"]:
        click.echo(f"  密钥已存入 {info['home']}/key（600）")
    else:
        click.secho(
            f"  ! 尚无密钥。设置环境变量 {settings.env_key}，"
            f"或重跑本命令带 --key-stdin",
            fg="yellow",
        )
    click.echo(f"\n你的 ~/.codex 未被改动。现在可以直接用：mcodex exec \"...\"")


@main.group()
def daemon():
    """带号池配置启停 multica daemon。

    multica 只能通过 MULTICA_CODEX_PATH 环境变量指定 codex 可执行文件，
    它的 config.json 不支持这个键。直接 `multica daemon start` 拿不到这个
    变量，daemon 就会退回系统 codex、绕开号池。

    这组命令在启动前把变量设好，避免依赖 shell profile 或人工记忆。
    """


def _daemon_env(settings) -> dict[str, str]:
    import os
    import shutil as _shutil

    found = _shutil.which("mcodex")
    if not found:
        _fail("PATH 里找不到 mcodex", "先 pip install -e .")
    return {**os.environ, "MULTICA_CODEX_PATH": found}


@daemon.command("start")
@click.pass_context
def daemon_start(ctx):
    """带 MULTICA_CODEX_PATH 启动 daemon。"""
    import subprocess

    env = _daemon_env(_settings(ctx))
    click.echo(f"MULTICA_CODEX_PATH = {env['MULTICA_CODEX_PATH']}")
    result = subprocess.run(["multica", "daemon", "start"], env=env)
    if result.returncode == 0:
        click.secho("✓ daemon 已带号池配置启动", fg="green")
        click.echo("  验证：multica daemon logs | grep 'agent version detected.*codex'")
    sys.exit(result.returncode)


@daemon.command("restart")
@click.pass_context
def daemon_restart(ctx):
    """重启 daemon 并重新带上号池配置。

    不用 `multica daemon restart`：那条命令由已在运行的 daemon 自己拉起
    新进程，会继承旧进程的环境，我们新设的变量传不进去。
    """
    import subprocess

    env = _daemon_env(_settings(ctx))
    subprocess.run(["multica", "daemon", "stop"], env=env)
    result = subprocess.run(["multica", "daemon", "start"], env=env)
    if result.returncode == 0:
        click.secho("✓ daemon 已重启并带上号池配置", fg="green")
    sys.exit(result.returncode)


@daemon.command("status")
def daemon_status():
    """显示 daemon 状态，并确认它用的是不是 mcodex。"""
    import re
    import subprocess

    subprocess.run(["multica", "daemon", "status"])

    log = Path.home() / ".multica" / "daemon.log"
    if not log.exists():
        return

    # 取最后一次探测到的 codex 路径
    pattern = re.compile(r"agent version detected.*name=codex.*path=(\S+)")
    last = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        found = pattern.search(line)
        if found:
            last = found.group(1)

    if last is None:
        click.echo("\n尚未在日志中看到 codex 探测记录")
    elif last.endswith("mcodex"):
        click.secho(f"\n✓ daemon 使用的 codex：{last}（走号池）", fg="green")
    else:
        click.secho(f"\n! daemon 使用的 codex：{last}（未走号池）", fg="yellow")
        click.echo("  用 poolctl daemon restart 让它改用 mcodex")


@mcodex.command("path")
def mcodex_path():
    """打印 mcodex 可执行文件路径，用于配置 MULTICA_CODEX_PATH。"""
    import shutil as _shutil

    found = _shutil.which("mcodex")
    if not found:
        click.secho("未在 PATH 中找到 mcodex。请先 pip install -e .", fg="yellow")
        sys.exit(1)
    click.echo(found)


# ---------------------------------------------------------------- status

@main.command()
@click.pass_context
def status(ctx):
    """显示当前配置与号池概况。"""
    settings = _settings(ctx)
    click.echo("配置：")
    for key, value in settings.redacted().items():
        click.echo(f"  {key:<20} {value}")

    click.echo("\n号池：")
    try:
        with GatewayClient(settings.pool_admin_url, settings.pool_admin_password) as gw:
            gw.login()
            accounts = gw.accounts()
            keys = gw.list_keys()
            click.echo(f"  账号 {len(accounts)} 个，密钥 {len(keys)} 把")
            for account in accounts:
                label = account.get("alias") or account.get("email") or account.get("id", "")[:8]
                click.echo(f"    账号 {label}  status={account.get('status')} plan={account.get('planType')}")
            for item in keys:
                state = "启用" if item.get("isActive") else "停用"
                click.echo(f"    密钥 {item.get('name'):<26}{state}  {item.get('keyPrefix')}")
    except (GatewayError, AuthRequired) as exc:
        click.secho(f"  网关不可用：{exc}", fg="yellow")


if __name__ == "__main__":
    main()
