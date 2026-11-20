#!/usr/bin/env python3
"""EasyShell MCP 一键认证配置工具 — 支持 Cursor / Claude Code / Codex / DSH 等。

用法:
  python easyshell_config.py --reinstall
  python easyshell_config.py --url http://127.0.0.1:8080 --password <pwd> --target all
  python easyshell_config.py --target cursor,claude,codex,dsh
  python easyshell_config.py --target dsh --from-config     # 复用已保存 Token（TeamServer 离线也能装）
  python easyshell_config.py --uninstall
  python easyshell_config.py --install-skills

DSH（DeepSeek Harness）说明:
  DSH 的 MCP server 不写在 IDE 配置里，而是走 profile 的 patch 层。本工具写入
  `$DSH_HOME/cordis.patch.yml`（home 级 patch，对本机全部 profile 生效），插入一条
  `mcp-easyshell` 行；dsh-tui 等 patchReload: live 的 profile 会热重载，无需重启。
  工具在对话中的名字形如 `mcp__easyshell__easyshell_exec_cmd`。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncio
    import httpx
except ImportError:
    print("[-] 需要 httpx: pip install httpx cryptography mcp")
    sys.exit(1)

_script_dir = Path(__file__).resolve().parent
_package_dir = _script_dir.parent
_project_root = _package_dir.parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from easyshell_api import EasyShellAPI

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MCP_NAME = "easyshell"
ALL_TARGETS = (
    "cursor",
    "claude",
    "claude-settings",
    "claude-mcp",
    "codex",
    "dsh",
    "project-cursor",
    "project-codex",
)


def banner():
    print("""
╔══════════════════════════════════════╗
║     EasyShell MCP 一键认证配置工具    ║
║ Cursor / Claude / Codex / DSH 多平台 ║
╚══════════════════════════════════════╝
""")


def get_input(prompt, default=None):
    if default:
        val = input(f"  {prompt} [{default}]: ").strip()
        return val if val else default
    return input(f"  {prompt}: ").strip()


def normalize_path_key(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def project_keys() -> list[str]:
    keys = [normalize_path_key(_project_root), normalize_path_key(Path.cwd())]
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


async def login_and_verify(base_url: str, username: str, password: str, use_basic: bool = False):
    basic = None
    if use_basic:
        basic = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    api = EasyShellAPI(base_url, basic_auth=basic)
    print(f"\n  → 登录 {api.api_root}/users/login ...")
    try:
        token = await api.login(username, password)
    except httpx.ConnectError:
        print(f"  ✗ 无法连接: {base_url}")
        return None, None, None
    except Exception as e:
        print(f"  ✗ 登录失败: {e}")
        return None, None, None

    api.token = token
    try:
        info = await api.get("client/server-runtime")
        if info.get("status") == 200:
            rt = info.get("data") or {}
            print("  ✓ 验证通过")
            print(f"    GOOS/GOARCH: {rt.get('goos')}/{rt.get('goarch')}")
        else:
            print(f"  ⚠ 验证响应: {info}")
    except Exception as e:
        print(f"  ⚠ 验证请求失败: {e}")

    return token, basic, api


def mcp_env_block(base_url: str, token: str, basic: str | None) -> dict:
    env = {
        "EASYSHELL_URL": base_url.rstrip("/"),
        "EASYSHELL_TOKEN": token,
    }
    if basic:
        env["EASYSHELL_BASICAUTH"] = basic
    return env


def mcp_json_entry(base_url: str, token: str, basic: str | None) -> dict:
    return {
        "command": "python",
        "args": [str(_script_dir / "server.py")],
        "env": mcp_env_block(base_url, token, basic),
    }


def mcp_claude_entry(base_url: str, token: str, basic: str | None) -> dict:
    entry = {
        "type": "stdio",
        "command": "python",
        "args": [str(_script_dir / "server.py")],
        "env": mcp_env_block(base_url, token, basic),
    }
    return entry


def _merge_json_mcp(path: Path, entry: dict) -> bool:
    data: dict = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})
    servers[MCP_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  ✓ 已写入 {path}")
    return True


def _remove_json_mcp(path: Path) -> bool:
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {})
    if MCP_NAME not in servers:
        return False
    del servers[MCP_NAME]
    data["mcpServers"] = servers
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  ✓ 已从 {path} 移除 {MCP_NAME}")
    return True


def register_cursor(base_url: str, token: str, basic: str | None) -> bool:
    return _merge_json_mcp(Path.home() / ".cursor" / "mcp.json", mcp_json_entry(base_url, token, basic))


def register_project_cursor(base_url: str, token: str, basic: str | None) -> bool:
    return _merge_json_mcp(_project_root / ".cursor" / "mcp.json", mcp_json_entry(base_url, token, basic))


def register_claude_settings(base_url: str, token: str, basic: str | None) -> bool:
    return _merge_json_mcp(Path.home() / ".claude" / "settings.json", mcp_json_entry(base_url, token, basic))


def register_claude_mcp(base_url: str, token: str, basic: str | None) -> bool:
    return _merge_json_mcp(Path.home() / ".claude" / "mcp.json", mcp_json_entry(base_url, token, basic))


def register_claude_projects(base_url: str, token: str, basic: str | None) -> bool:
    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        print(f"  ⚠ 未找到 {claude_json}，跳过 Claude 项目级配置")
        return False
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    projects = data.setdefault("projects", {})
    entry = mcp_claude_entry(base_url, token, basic)
    written = False
    for key in project_keys():
        proj = projects.setdefault(key, {})
        proj.setdefault("mcpServers", {})[MCP_NAME] = entry
        written = True
        print(f"  ✓ 已写入 projects['{key}'].mcpServers")
    data["projects"] = projects
    claude_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return written


def _strip_codex_easyshell(text: str) -> str:
    text = re.sub(r"\n\[mcp_servers\.easyshell\][^\[]*", "", text, flags=re.DOTALL)
    text = re.sub(r"\n\[mcp_servers\.easyshell\.env\][^\[]*", "", text, flags=re.DOTALL)
    return text.rstrip() + "\n"


def register_codex(base_url: str, token: str, basic: str | None) -> bool:
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_path = codex_dir / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    existing = _strip_codex_easyshell(existing)
    env = mcp_env_block(base_url, token, basic)
    server_path = str(_script_dir / "server.py").replace("\\", "/")
    block = f"\n[mcp_servers.{MCP_NAME}]\ncommand = \"python\"\nargs = [\"{server_path}\"]\n\n[mcp_servers.{MCP_NAME}.env]\n"
    for k, v in env.items():
        block += f'{k} = "{v}"\n'
    config_path.write_text(existing + block, encoding="utf-8")
    print(f"  ✓ 已写入 {config_path}")
    return True


def register_project_codex(base_url: str, token: str, basic: str | None) -> bool:
    codex_dir = _project_root / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_path = codex_dir / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    existing = _strip_codex_easyshell(existing)
    env = mcp_env_block(base_url, token, basic)
    server_path = str(_script_dir / "server.py").replace("\\", "/")
    block = f"\n[mcp_servers.{MCP_NAME}]\ncommand = \"python\"\nargs = [\"{server_path}\"]\n\n[mcp_servers.{MCP_NAME}.env]\n"
    for k, v in env.items():
        block += f'{k} = "{v}"\n'
    config_path.write_text(existing + block, encoding="utf-8")
    print(f"  ✓ 已写入 {config_path}")
    return True


# ══════════════════════════════════════════════════════════════
# DSH（DeepSeek Harness）— home 级 patch 层 $DSH_HOME/cordis.patch.yml
#   patch 行只做「按 id 覆盖 / 禁用 / insert 插入」，插入新插件必须写成
#   - insert:
#       - id: ...
#   空文件或纯注释文件会让 dsh 启动失败，因此停用时写 [] 而不是删文件。
# ══════════════════════════════════════════════════════════════
DSH_ROW_ID = "mcp-easyshell"
DSH_PLUGIN = "@deepseek-ai/dsh-mcp-client"
DSH_SERVER_NAME = MCP_NAME
DSH_PATCH_FILENAME = "cordis.patch.yml"
DSH_SKILL_MARKER = ".easyshell-managed"
DSH_HEADER_COMMENT = f"# {DSH_PATCH_FILENAME} — DSH home 级 patch 层（对本机全部 profile 生效，由 easyshell_config.py 维护）"
DSH_EMPTY_COMMENT = "# 当前无 patch（空文件会让 dsh 启动失败，故保留 []）"
DSH_MANAGED_COMMENTS = (DSH_HEADER_COMMENT, DSH_EMPTY_COMMENT)


def dsh_home(explicit: str | Path | None = None) -> Path | None:
    """返回 DSH home 目录；未装 DSH 且未显式指定时返回 None。"""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env).expanduser()
    default = Path.home() / ".dsh"
    return default if default.exists() else None


def dsh_patch_path(home: Path) -> Path:
    return home / DSH_PATCH_FILENAME


def dsh_skill_root(home: Path) -> Path:
    """DSH 用户级 skill 根目录（rank 400，对全部会话可见）。"""
    return home / "skills"


def dsh_profiles(home: Path) -> list[str]:
    """已初始化的 profile 名字列表（有 package.json 的才算）。"""
    root = home / "profiles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "package.json").is_file())


def python_command() -> str:
    """DSH 会清洗子进程环境，用绝对解释器路径而不是裸 python。"""
    exe = sys.executable or shutil.which("python") or "python"
    return str(exe)


def _posix(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def _yq(value: str) -> str:
    """YAML 单引号标量：单引号内的反斜杠、冒号、# 都是字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


def dsh_patch_block(base_url: str, token: str, basic: str | None) -> str:
    env = mcp_env_block(base_url, token, basic)
    lines = [
        "- insert:",
        f"    - id: {DSH_ROW_ID}",
        f"      name: {_yq(DSH_PLUGIN)}",
        "      config:",
        f"        serverName: {DSH_SERVER_NAME}",
        "        transport: stdio",
        f"        command: {_yq(python_command())}",
        "        args:",
        f"          - {_yq(_posix(_script_dir / 'server.py'))}",
        "        env:",
    ]
    for k, v in env.items():
        lines.append(f"          {k}: {_yq(v)}")
    lines += [
        f"        cwd: {_yq(_posix(_script_dir))}",
        "        toolCallTimeoutMs: 300000",
        "        failOnStartupError: false",
    ]
    return "\n".join(lines) + "\n"


def _dsh_patch_has_entries(text: str) -> bool:
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.startswith("- "):
            return True
    return False


def _strip_dsh_patch(text: str) -> str:
    """删掉 mcp-easyshell 行（含其缩进子块），保留文件里其它 patch 行。"""
    pattern = re.compile(r"^(\s*)-\s*id:\s*[\"']?" + re.escape(DSH_ROW_ID) + r"[\"']?\s*$")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = pattern.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent = len(m.group(1))
        i += 1
        while i < len(lines):
            cur = lines[i]
            if not cur.strip():
                break
            if (len(cur) - len(cur.lstrip())) > indent:
                i += 1
                continue
            break
    return "\n".join(out)


def _prune_empty_inserts(text: str) -> str:
    """清理只剩壳的 `- insert:`（子项被删光后会退化成无效 patch）。"""
    lines = text.splitlines()
    out: list[str] = []
    for idx, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s*insert:\s*$", line)
        if m:
            indent = len(m.group(1))
            j = idx + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or (len(lines[j]) - len(lines[j].lstrip())) <= indent:
                continue
        out.append(line)
    return "\n".join(out)


def _dsh_patch_render(text: str, *, fallback_comment: str = DSH_EMPTY_COMMENT) -> str:
    """把 patch 文本收敛成「合法 YAML 列表」：空则补 []，否则去掉占位 []。"""
    lines = [ln for ln in text.splitlines() if ln.strip() != "[]"]
    while lines and not lines[-1].strip():
        lines.pop()
    if _dsh_patch_has_entries("\n".join(lines)):
        return "\n".join(lines).rstrip("\n") + "\n"
    comments = [ln for ln in lines if ln.strip().startswith("#") and ln.strip() != fallback_comment]
    comments.append(fallback_comment)
    return "\n".join(comments) + "\n[]\n"


def register_dsh(base_url: str, token: str, basic: str | None, home: Path | None = None) -> bool:
    home = home or dsh_home()
    if home is None:
        print("  ⚠ 未检测到 DSH（$DSH_HOME 与 ~/.dsh 都不存在），跳过 DSH")
        print("    指定安装位置：--dsh-home <DSH_HOME 路径>")
        return False
    path = dsh_patch_path(home)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    kept = _prune_empty_inserts(_strip_dsh_patch(existing))
    # 丢掉本工具自己写的注释与占位符，重装时才不会越写越长
    lines = [
        ln for ln in kept.splitlines()
        if ln.strip() != "[]" and ln.strip() not in DSH_MANAGED_COMMENTS
    ]
    while lines and not lines[-1].strip():
        lines.pop()
    if _dsh_patch_has_entries("\n".join(lines)):
        head = "\n".join(lines).rstrip("\n") + "\n"
    else:
        comments = [ln for ln in lines if ln.strip().startswith("#")]
        comments.append(DSH_HEADER_COMMENT)
        head = "\n".join(comments) + "\n"
    body = head + dsh_patch_block(base_url, token, basic)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"  ✓ 已写入 {path}")
    print(f"    id={DSH_ROW_ID} → 工具名 mcp__{DSH_SERVER_NAME}__easyshell_*（live profile 免重启）")
    return True


def unregister_dsh(home: Path | None = None) -> bool:
    home = home or dsh_home()
    if home is None:
        return False
    path = dsh_patch_path(home)
    changed = False
    if path.exists():
        text = path.read_text(encoding="utf-8")
        stripped = _prune_empty_inserts(_strip_dsh_patch(text))
        if stripped != text:
            path.write_text(_dsh_patch_render(stripped), encoding="utf-8")
            print(f"  ✓ 已从 {path} 移除 {DSH_ROW_ID}")
            changed = True
    if uninstall_dsh_skills(home):
        changed = True
    return changed


def sync_dsh_skills(home: Path | None = None) -> list[str]:
    """把 skills 装进 $DSH_HOME/skills/<name>/SKILL.md（DSH 的 skill bundle 约定）。"""
    home = home or dsh_home()
    if home is None:
        return []
    root = dsh_skill_root(home)
    written: list[str] = []
    for name, src in skill_sources().items():
        if not src.exists():
            print(f"  ⚠ 跳过 DSH skill {name}，源文件不存在: {src}")
            continue
        dst_dir = root / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / "SKILL.md")
        (dst_dir / DSH_SKILL_MARKER).write_text("managed by easyshell_config.py\n", encoding="utf-8")
        written.append(name)
        print(f"  ✓ DSH skills/{name}/SKILL.md")
    if written:
        print(f"    会话中直接 @{written[0]} 或加载对应 skill 即可")
    return written


def uninstall_dsh_skills(home: Path | None = None) -> bool:
    home = home or dsh_home()
    if home is None:
        return False
    root = dsh_skill_root(home)
    if not root.is_dir():
        return False
    removed = False
    for child in root.iterdir():
        if child.is_dir() and (child / DSH_SKILL_MARKER).exists():
            shutil.rmtree(child, ignore_errors=True)
            print(f"  ✓ 已移除 {child}")
            removed = True
    return removed


def dsh_cli_entry() -> Path | None:
    """定位 dsh 启动器本体（node 直接跑 lib/bin.js，绕开 .ps1/.cmd 包装）。"""
    candidates: list[Path] = []
    which = shutil.which("dsh")
    if which:
        candidates.append(Path(which).resolve().parent)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm")
    for base in candidates:
        entry = base / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        if entry.is_file():
            return entry
    return None


def verify_dsh(home: Path, profile: str | None = None, timeout: int = 90) -> bool:
    """跑一次 dsh --dump-config，确认 patch 能被组合进 profile（真解析，不是猜）。"""
    node = shutil.which("node")
    entry = dsh_cli_entry()
    if not node or not entry:
        print("  ⚠ 未找到 node/dsh，跳过 DSH 配置校验（可手动 dsh --profile <名> --dump-config）")
        return False
    profiles = dsh_profiles(home)
    target = profile or ("dsh-tui" if "dsh-tui" in profiles else (profiles[0] if profiles else None))
    if not target:
        print("  ⚠ 还没有已初始化的 profile，跳过校验（首次启动 DSH 后会自动生效）")
        return False
    print(f"  → 校验 dsh --profile {target} --dump-config ...")
    try:
        proc = subprocess.run(
            [node, str(entry), "--profile", target, "--dump-config"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "DSH_TELEMETRY_DISABLED": "1"},
        )
    except Exception as e:
        print(f"  ⚠ 校验失败（不影响已写入的配置）: {e}")
        return False
    out = proc.stdout or ""
    err = proc.stderr or ""
    if proc.returncode == 0 and f"id: {DSH_ROW_ID}" in out:
        print(f"  ✓ DSH 已能组合出 {DSH_ROW_ID}（profile: {target}）")
        return True
    print(f"  ✗ DSH 校验未通过（exit={proc.returncode}）")
    for line in (err.strip().splitlines() or ["(无 stderr)"])[:6]:
        print(f"    {line}")
    return False


def unregister_claude_projects() -> bool:
    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        return False
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    found = False
    for proj_val in data.get("projects", {}).values():
        if not isinstance(proj_val, dict):
            continue
        for key in ("mcpServers", "disabledMcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers"):
            bucket = proj_val.get(key)
            if isinstance(bucket, dict) and MCP_NAME in bucket:
                del bucket[MCP_NAME]
                found = True
            elif isinstance(bucket, list) and MCP_NAME in bucket:
                bucket.remove(MCP_NAME)
                found = True
    if found:
        claude_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  ✓ 已清理 {claude_json}")
    return found


def unregister_codex(path: Path) -> bool:
    if not path.exists():
        return False
    text = _strip_codex_easyshell(path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    print(f"  ✓ 已清理 {path}")
    return True


def unregister_all() -> None:
    unregister_targets(list(ALL_TARGETS))


def unregister_targets(targets: list[str]) -> None:
    """只清理指定目标，避免 `--reinstall --target dsh` 顺手删掉别的平台。"""
    mapping = {
        "cursor": lambda: _remove_json_mcp(Path.home() / ".cursor" / "mcp.json"),
        "claude": unregister_claude_projects,
        "claude-settings": lambda: _remove_json_mcp(Path.home() / ".claude" / "settings.json"),
        "claude-mcp": lambda: _remove_json_mcp(Path.home() / ".claude" / "mcp.json"),
        "codex": lambda: unregister_codex(Path.home() / ".codex" / "config.toml"),
        "dsh": unregister_dsh,
        "project-cursor": lambda: _remove_json_mcp(_project_root / ".cursor" / "mcp.json"),
        "project-codex": lambda: unregister_codex(_project_root / ".codex" / "config.toml"),
    }
    for t in targets:
        fn = mapping.get(t)
        if fn:
            fn()


def register_targets(targets: list[str], base_url: str, token: str, basic: str | None, dsh_home_dir: Path | None = None) -> None:
    mapping = {
        "cursor": register_cursor,
        "claude": register_claude_projects,
        "claude-settings": register_claude_settings,
        "claude-mcp": register_claude_mcp,
        "codex": register_codex,
        "dsh": lambda u, t, b: register_dsh(u, t, b, home=dsh_home_dir),
        "project-cursor": register_project_cursor,
        "project-codex": register_project_codex,
    }
    for t in targets:
        fn = mapping.get(t)
        if fn:
            print(f"\n  → 写入 {t} ...")
            fn(base_url, token, basic)


def load_local_config() -> dict | None:
    """读取上一次写入的 config（含可复用 Token）。"""
    config_path = _script_dir / "config"
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_local_config(base_url: str, token: str, basic: str | None, *, clients_online: int = 0, default_uid: str = "") -> Path:
    config_path = _script_dir / "config"
    env = mcp_env_block(base_url, token, basic)
    payload = {
        **env,
        "clients_online": clients_online,
        "default_uid": default_uid,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setup_method": "easyshell_config.py",
        "supported_targets": list(ALL_TARGETS),
        "note": "含真实 Token，勿提交 git。重装: python easyshell_config.py --reinstall",
        "mcpServers": {MCP_NAME: mcp_json_entry(base_url, token, basic)},
    }
    config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  ✓ 已写入 {config_path}")
    return config_path


def skill_sources() -> dict[str, Path]:
    """skill 名 → 仓库内权威源文件（Cursor / DSH 共用同一份）。"""
    return {
        "easyshell-mcp": _script_dir / "easyshell-mcp" / "skill.md",
        "easyshell-real": _script_dir / "easyshell-real" / "skill.md",
        "easyshell-lateral": _script_dir / "easyshell-lateral" / "skill.md",
        "easyshell-phishing": _script_dir / "easyshell-phishing" / "skill.md",
        "easyshell-payload": _script_dir / "easyshell-payload" / "skill.md",
        "easyshell-pentest": _package_dir / "skill" / "easyshell-pentest.md",
    }


def install_skills() -> None:
    """同步 skills 到项目 .cursor/skills + $DSH_HOME/skills，并维护 AGENTS.md。"""
    dst_root = _project_root / ".cursor" / "skills"
    dst_root.mkdir(parents=True, exist_ok=True)

    for name, src in skill_sources().items():
        if not src.exists():
            print(f"  ⚠ 跳过 {name}，源文件不存在: {src}")
            continue
        dst_dir = dst_root / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "SKILL.md"
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
            print(f"  ✓ skills/{name}/SKILL.md")

    agents_md = _project_root / "AGENTS.md"
    content = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    marker = "<!-- easyshell-mcp-skills -->"
    block = f"""{marker}
## EasyShell MCP Skills

安装 MCP 后，在对话中加载以下技能：

| Skill | 用途 |
|-------|------|
| easyshell-mcp | MCP 工具与 API 映射 |
| easyshell-real | C2 实战 SOP |
| easyshell-lateral | 内网横向 |
| easyshell-phishing | 邮件钓鱼演练 |
| easyshell-payload | 载荷生成与 Web 投递 |
| easyshell-pentest | 架构与漏洞链 |

工具名：Cursor/Claude/Codex 为 `easyshell_*`，DSH 为 `mcp__easyshell__easyshell_*`。

文档入口：`easyshell-package/README.md`
"""
    if marker not in content:
        agents_md.write_text((content.rstrip() + "\n\n" + block + "\n"), encoding="utf-8")
        print(f"  ✓ 已更新 {agents_md}")
    else:
        # 更新已有段落（追加新 skill 行等）
        new_content = re.sub(
            r"<!-- easyshell-mcp-skills -->.*",
            block.rstrip() + "\n",
            content,
            count=1,
            flags=re.DOTALL,
        )
        if new_content != content:
            agents_md.write_text(new_content, encoding="utf-8")
            print(f"  ✓ 已刷新 {agents_md}")
        else:
            print(f"  ℹ {agents_md} 已包含 EasyShell skills 段落")

    # DSH 的 skill 走 $DSH_HOME/skills（frontmatter 已就绪，直接是合法 bundle）
    sync_dsh_skills()


async def test_api(api: EasyShellAPI) -> None:
    print("  ──────────────────────────────────────────")
    print("  🔍 功能测试")
    print("  ──────────────────────────────────────────")
    tests = []

    async def _check(name: str, coro):
        try:
            r = await coro
            if r.get("status") == 200:
                tests.append((name, "✓"))
            else:
                tests.append((name, f"✗ {r.get('status')} {r.get('data', r.get('error', ''))}"))
        except Exception as e:
            tests.append((name, f"✗ {e}"))

    await _check("Server Runtime", api.get("client/server-runtime"))
    try:
        r = await api.get("client/clientslist", params={"page": 1, "page_size": 5})
        if r.get("status") == 200:
            total = (r.get("data") or {}).get("total", 0)
            tests.append(("客户端列表", f"✓ total={total}"))
        else:
            tests.append(("客户端列表", f"✗ {r}"))
    except Exception as e:
        tests.append(("客户端列表", f"✗ {e}"))
    await _check("SOCKS5 API", api.get("socks5/list", params={"page": 1, "page_size": 5}))
    await _check("监听器", api.get("listener/list"))

    for name, status in tests:
        print(f"    {name:20s}  {status}")
    print()


def parse_targets(raw: str | None) -> list[str]:
    if not raw or raw == "all":
        return list(ALL_TARGETS)
    return [t.strip() for t in raw.split(",") if t.strip()]


def resolve_dsh_home(args) -> Path | None:
    home = dsh_home(getattr(args, "dsh_home", None))
    if home is None and Path.home().joinpath(".dsh").exists():
        home = Path.home() / ".dsh"
    return home


async def main_async(args):
    dsh_home_dir = resolve_dsh_home(args)

    if args.install_skills:
        print("\n  → 安装 Skills ...")
        install_skills()   # 含 $DSH_HOME/skills
        return

    if args.uninstall:
        unregister_all()
        print("\n  [OK] 卸载完成，请重启各 IDE/CLI 后生效（DSH 为热重载，立即生效）")
        return

    targets = parse_targets(args.target)
    reuse = load_local_config() if (args.from_config or getattr(args, "reuse_token", False)) else None

    if reuse and reuse.get("EASYSHELL_TOKEN"):
        base_url = args.url or reuse.get("EASYSHELL_URL") or "http://127.0.0.1:8080"
        token = reuse["EASYSHELL_TOKEN"]
        basic = reuse.get("EASYSHELL_BASICAUTH")
        api = None
        print(f"\n  ℹ 复用已保存凭据 {_script_dir / 'config'}（未登录，Token 过期请重跑不带 --from-config）")
    else:
        base_url = args.url or get_input("EasyShell 地址", "http://127.0.0.1:8080")
        username = args.username or get_input("用户名", "")
        password = args.password or get_input("密码", "")
        token, basic, api = await login_and_verify(base_url, username, password, use_basic=args.basic)
        if not token:
            print("\n  ✗ 登录失败。TeamServer 暂时不可达时，可先用已有 Token 安装：")
            print("     python easyshell_config.py --target dsh --from-config")
            sys.exit(1)

    clients_online = 0
    default_uid = ""
    if api and not args.no_test:
        await test_api(api)
        try:
            r = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
            if r.get("status") == 200:
                lst = (r.get("data") or {}).get("list") or []
                clients_online = sum(1 for c in lst if str(c.get("Online", "")).strip() == "1")
                online = [c for c in lst if str(c.get("Online", "")).strip() == "1"]
                if online:
                    default_uid = online[0].get("UID") or online[0].get("uid") or ""
        except Exception:
            pass
    elif reuse:
        clients_online = reuse.get("clients_online", 0) or 0
        default_uid = reuse.get("default_uid", "") or ""

    save_local_config(base_url, token, basic, clients_online=clients_online, default_uid=default_uid)

    if args.reinstall or args.write_project or args.write_all or args.target is not None:
        if args.reinstall:
            # DSH 不预清理：register_dsh 原地替换自己那一行，只写一次文件。
            # （先清空再写回是两次写入，live watcher 可能把它们合并成一次，
            #   结果条目被删掉却没加回来——表现就是「重启后工具又找不见」。）
            todo = list(ALL_TARGETS) if args.target is None else targets
            print("  → 清理旧配置 ...")
            unregister_targets([t for t in todo if t != "dsh"])
        register_targets(targets, base_url, token, basic, dsh_home_dir=dsh_home_dir)
        install_skills()

    if "dsh" in targets and not args.no_dsh_verify and dsh_home_dir:
        print("\n  → DSH 校验 ...")
        verify_dsh(dsh_home_dir, profile=args.dsh_profile)

    print("  ──────────────────────────────────────────")
    print("  环境变量（手动配置时可复制）:")
    for k, v in mcp_env_block(base_url, token, basic).items():
        print(f"    set {k}={v}")
    if default_uid:
        print(f"    set EASYSHELL_UID={default_uid}")
    print()
    print("  已支持平台: Cursor / Claude Code / Codex / DSH")
    print("  Cursor: 设置 → MCP → 重启")
    print("  Claude: 重启后 /mcp 检查 easyshell_* 工具")
    print("  Codex: 重启 CLI 或 /mcp 检查")
    print("  DSH: 工具名 mcp__easyshell__easyshell_*，live profile 免重启")
    print("  Skills: python easyshell_config.py --install-skills")
    return


def main():
    banner()
    parser = argparse.ArgumentParser(
        description="EasyShell MCP 多平台配置",
        epilog="示例:\n"
               "  python easyshell_config.py --reinstall\n"
               "  python easyshell_config.py --target cursor,claude,codex,dsh --password xxx\n"
               "  python easyshell_config.py --target dsh --from-config      # TeamServer 离线也能装 DSH\n"
               "  python easyshell_config.py --install-skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-u", "--url", help="TeamServer 地址")
    parser.add_argument("--username", help="用户名")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--basic", action="store_true", help="登录时附带 HTTP Basic")
    parser.add_argument("--target", default=None, help=f"写入目标: all 或逗号分隔 {','.join(ALL_TARGETS)}")
    parser.add_argument("--write-project", action="store_true", help="写入项目级与全部平台（同 --target all）")
    parser.add_argument("--write-all", action="store_true", help="写入全部平台")
    parser.add_argument("--reinstall", action="store_true", help="卸载后重装全部平台")
    parser.add_argument("--uninstall", action="store_true", help="从全部平台移除")
    parser.add_argument("--install-skills", action="store_true", help="仅安装/同步 skills 与 AGENTS.md")
    parser.add_argument("--from-config", "--reuse-token", dest="from_config", action="store_true",
                        help="复用 easyshell-mcp/config 里已保存的 Token，不重新登录")
    parser.add_argument("--dsh-home", help=f"DSH home 目录（默认 $DSH_HOME 或 ~/.dsh）")
    parser.add_argument("--dsh-profile", help="DSH 校验用的 profile 名（默认 dsh-tui）")
    parser.add_argument("--no-dsh-verify", action="store_true", help="跳过 dsh --dump-config 校验")
    parser.add_argument("--no-test", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
