"""mcodex —— 走公司号池的独立 codex 封装。

设计目标：**完全不碰用户的 codex**。
- 用户的 `codex` 与 `~/.codex/config.toml` 保持原样，不注入任何东西。
- `mcodex` 使用自己的 CODEX_HOME（默认 ~/.mcodex），号池 provider 只写在那里。
- 两者可以并存：`codex` 走个人 ChatGPT 订阅，`mcodex` 走公司号池。

同一个可执行文件覆盖两种运行场景：

1. **人直接敲 `mcodex`**：环境里没有 CODEX_HOME，用 ~/.mcodex。
2. **multica daemon 拉起**：daemon 会把 CODEX_HOME 指向 per-task 目录，并从宿主
   ~/.codex/config.toml 拷一份配置进去。既然宿主那份不再含号池 provider，
   mcodex 就在 exec 前把 provider 注入这份 per-task 配置。daemon 准备完
   codex-home 后不再改它，所以这个时机是安全的，且每个 task 都是新目录。

两种场景下都靠 os.execvp 原样透传 argv 与 stdio —— daemon 用的是
`app-server --listen stdio://`，参数被 codexBlockedArgs 硬校验，
stdio 也是它与 codex 通信的唯一通道，任何包装、缓冲或改写都会让链路失效。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .codex_config import ProviderSpec, inject

#: mcodex 自己的家目录，与用户的 ~/.codex 完全隔离
DEFAULT_MCODEX_HOME = Path.home() / ".mcodex"

#: 密钥文件。权限应为 600。也可用环境变量直接提供。
KEY_FILENAME = "key"

#: 记录真 codex 的绝对路径。
#: 必须固化，不能每次靠 PATH 顺序现找——daemon 的 PATH 与登录 shell 往往不同，
#: 同一台机器上可能装了多个 codex（nvm 的、npm 全局的、/usr/local 的），
#: 现找会导致"你手动跑和 multica 跑用的不是同一个二进制"。
REAL_CODEX_FILENAME = "real-codex"

#: 从宿主 codex 配置继承的键。只取使用偏好，不取任何凭据或 provider 定义。
INHERITED_KEYS = (
    "model",
    "model_reasoning_effort",
    "personality",
    "approval_policy",
    "plan_mode_reasoning_effort",
)


class McodexError(RuntimeError):
    pass


def mcodex_home() -> Path:
    """mcodex 自己的家目录。可用 MCODEX_HOME 覆盖。"""
    override = os.environ.get("MCODEX_HOME")
    return Path(override) if override else DEFAULT_MCODEX_HOME


def discover_codex_in_path() -> str | None:
    """在 PATH 里找第一个不是本封装自身的 codex。找不到返回 None。"""
    me = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "codex"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            if me and candidate.resolve() == me:
                continue  # 就是我们自己，跳过，否则无限递归
        except OSError:
            continue
        return str(candidate)
    return None


def find_real_codex(home: Path | None = None) -> str:
    """定位真正的 codex 可执行文件。

    解析顺序刻意做成确定性的：

    1. `MCODEX_REAL_CODEX` —— 临时覆盖，便于排障与灰度
    2. `<home>/real-codex` —— init 时固化下来的绝对路径
    3. PATH 探测 —— 仅作兜底

    第 2 步是关键。daemon 的 PATH 与登录 shell 常常不同，一台机器上又可能同时
    存在 nvm、npm 全局、/usr/local 等多份 codex；只靠 PATH 现找，会出现
    "手动跑 mcodex 用 A，multica 跑用 B"这种难以察觉的分叉。
    """
    explicit = os.environ.get("MCODEX_REAL_CODEX")
    if explicit:
        if not Path(explicit).exists():
            raise McodexError(f"MCODEX_REAL_CODEX 指向的文件不存在：{explicit}")
        return explicit

    home = home or mcodex_home()
    pinned_file = home / REAL_CODEX_FILENAME
    if pinned_file.exists():
        pinned = pinned_file.read_text(encoding="utf-8").strip()
        if pinned and Path(pinned).is_file() and os.access(pinned, os.X_OK):
            return pinned
        # 记录的路径失效（codex 升级/卸载）时不能静默换一个，
        # 否则又回到了不确定的老问题上。
        raise McodexError(
            f"{pinned_file} 记录的 codex 已不可用：{pinned}\n"
            "重新固定：poolctl mcodex init --real-codex <绝对路径>"
        )

    found = discover_codex_in_path()
    if found:
        return found

    raise McodexError(
        "PATH 里找不到 codex。请安装 codex CLI，"
        "或用 poolctl mcodex init --real-codex <绝对路径> 指定。"
    )


def read_key(home: Path) -> str | None:
    """取号池密钥：环境变量优先，其次密钥文件。"""
    from_env = os.environ.get("GW_API_KEY")
    if from_env:
        return from_env

    key_file = home / KEY_FILENAME
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def init_home(
    home: Path,
    spec: ProviderSpec,
    *,
    inherit_from: Path | None = None,
    key: str | None = None,
    real_codex: str | None = None,
) -> dict[str, object]:
    """初始化 mcodex 家目录：建目录、写 provider 配置、可选存密钥。

    inherit_from 指向宿主 ~/.codex/config.toml 时，会复制其中的使用偏好
    （模型、推理强度、信任目录等），让 mcodex 用起来和平时一致。
    绝不复制 auth.json 或任何凭据。
    """
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    config_path = home / "config.toml"
    inherited: list[str] = []

    if inherit_from and inherit_from.exists() and not config_path.exists():
        import tomllib

        source = tomllib.loads(inherit_from.read_text(encoding="utf-8"))
        lines = []
        for key_name in INHERITED_KEYS:
            if key_name in source:
                lines.append(f'{key_name} = "{source[key_name]}"')
                inherited.append(key_name)
        # 信任目录也带过来，否则每个目录都要重新确认
        for project, settings in (source.get("projects") or {}).items():
            level = (settings or {}).get("trust_level")
            if level:
                lines.append(f'\n[projects."{project}"]\ntrust_level = "{level}"')
        if lines:
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = inject(spec, path=config_path, backup=False)

    stored_key = False
    if key:
        key_file = home / KEY_FILENAME
        key_file.write_text(key.strip() + "\n", encoding="utf-8")
        key_file.chmod(0o600)
        stored_key = True

    # 固化真 codex 路径，让 daemon 与登录 shell 用的一定是同一个二进制。
    # 只取绝对路径，**不跟随软链**：nvm / npm 全局装出来的 bin/codex 本身就是
    # 软链，跟随它会绑到底层的 codex.js 或某个版本目录，反而在升级后失效。
    resolved = real_codex or discover_codex_in_path()
    if resolved:
        resolved = str(Path(resolved).absolute())
        if not (Path(resolved).is_file() and os.access(resolved, os.X_OK)):
            raise McodexError(f"指定的 codex 不可执行：{resolved}")
        (home / REAL_CODEX_FILENAME).write_text(resolved + "\n", encoding="utf-8")

    return {
        "home": home,
        "config": config_path,
        "inherited": inherited,
        "config_changed": result.changed,
        "key_stored": stored_key,
        "real_codex": resolved,
    }


def prepare_env(spec: ProviderSpec) -> dict[str, str]:
    """算出 exec 前要设置的环境变量，并按需注入 provider 配置。

    返回要追加/覆盖的环境变量。不直接改 os.environ，便于测试。
    """
    updates: dict[str, str] = {}

    daemon_home = os.environ.get("CODEX_HOME")
    if daemon_home:
        # multica 场景：daemon 已把 CODEX_HOME 指向 per-task 目录并拷好配置。
        # 不能改 CODEX_HOME（daemon 的 isBlockedEnvKey 也不允许用户覆盖），
        # 只往它准备好的那份配置里补上号池 provider。
        target = Path(daemon_home)
        home_for_key = mcodex_home()
        if target.is_dir():
            inject(spec, path=target / "config.toml", backup=False)
    else:
        # 人直接用：切到 mcodex 自己的家目录
        home_for_key = mcodex_home()
        if not (home_for_key / "config.toml").exists():
            raise McodexError(
                f"{home_for_key} 尚未初始化。先运行：poolctl mcodex init --key <密钥>"
            )
        updates["CODEX_HOME"] = str(home_for_key)

    if not os.environ.get(spec.env_key):
        key = read_key(home_for_key)
        if not key:
            raise McodexError(
                f"没有号池密钥。请设置环境变量 {spec.env_key}，"
                f"或把密钥写入 {home_for_key / KEY_FILENAME}（权限 600）。\n"
                "签发：poolctl issue <你的标识>"
            )
        updates[spec.env_key] = key

    return updates


def main(argv: list[str] | None = None) -> int:
    """入口：准备环境后用 execvp 交棒给真 codex。"""
    from .config import Settings

    args = list(argv if argv is not None else sys.argv[1:])
    settings = Settings.load()
    spec = ProviderSpec(
        provider_id=settings.provider_id,
        base_url=settings.pool_base_url,
        env_key=settings.env_key,
    )

    try:
        updates = prepare_env(spec)
        real_codex = find_real_codex(mcodex_home())
    except McodexError as exc:
        print(f"mcodex: {exc}", file=sys.stderr)
        return 1

    os.environ.update(updates)

    # execvp 替换当前进程：argv 与 stdio 原样交给真 codex。
    # daemon 用 `app-server --listen stdio://` 通过 stdio 通信，
    # 中间加任何包装或缓冲都会让链路失效。
    os.execvp(real_codex, [real_codex, *args])


if __name__ == "__main__":
    sys.exit(main())
