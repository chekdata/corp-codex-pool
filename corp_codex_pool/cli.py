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
from .gateway import AuthRequired, GatewayClient, GatewayError, summarize_by_key
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
@click.option("--json", "as_json", is_flag=True, help="输出 JSON")
@click.pass_context
def usage(ctx, since, until, max_records, as_json):
    """按人汇总用量。归属依据是密钥，密钥由 poolctl issue 与员工绑定。"""
    settings = _settings(ctx)
    try:
        with GatewayClient(settings.pool_admin_url, settings.pool_admin_password) as gw:
            gw.login()
            rows = gw.request_logs(since=since, until=until, max_records=max_records)
    except GatewayError as exc:
        _fail(f"拉取用量失败：{exc}")
        return

    summary = summarize_by_key(rows)

    if as_json:
        click.echo(jsonlib.dumps(summary, ensure_ascii=False, indent=2))
        return

    if not summary:
        click.echo("时间范围内没有请求记录")
        return

    click.echo(f"共 {len(rows)} 条请求，按密钥归集：\n")
    header = f"{'密钥':<28}{'请求':>6}{'输入':>12}{'缓存':>12}{'输出':>10}{'命中率':>9}{'成本USD':>11}"
    click.echo(header)
    click.echo("-" * len(header))

    for bucket in sorted(summary.values(), key=lambda b: -b["costUsd"]):
        name = bucket["apiKeyName"] or "(未归属)"
        rate = f"{bucket['cacheHitRate']:.1%}" if bucket["cacheHitRate"] is not None else "—"
        click.echo(
            f"{name:<28}{bucket['requests']:>6}{bucket['inputTokens']:>12,}"
            f"{bucket['cachedInputTokens']:>12,}{bucket['outputTokens']:>10,}"
            f"{rate:>9}{bucket['costUsd']:>11.4f}"
        )

    total = sum(b["costUsd"] for b in summary.values())
    click.echo("-" * len(header))
    click.echo(f"{'合计':<28}{len(rows):>6}{'':>12}{'':>12}{'':>10}{'':>9}{total:>11.4f}")


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
            checks.append(
                doctor_mod.Check(
                    "已下发密钥的 agent",
                    doctor_mod.OK if delivered else doctor_mod.WARN,
                    f"{len(delivered)}/{len(agents)} 个 agent 带有 {settings.env_key}",
                    "运行 poolctl issue <员工> --agent-id <id> 下发",
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
