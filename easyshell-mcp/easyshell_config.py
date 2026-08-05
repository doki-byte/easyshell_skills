#!/usr/bin/env python3
"""EasyShell MCP 一键认证配置工具 — 支持 Cursor / Claude Code / Codex 等。

用法:
  python easyshell_config.py --reinstall
  python easyshell_config.py --url http://127.0.0.1:8080 --password <pwd> --target all
  python easyshell_config.py --target cursor,claude,codex
  python easyshell_config.py --uninstall
  python easyshell_config.py --install-skills
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
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
ALL_TARGETS = ("cursor", "claude", "claude-settings", "claude-mcp", "codex", "project-cursor", "project-codex")


def banner():
    print("""
╔══════════════════════════════════════╗
║     EasyShell MCP 一键认证配置工具    ║
║  Cursor / Claude / Codex 多平台写入  ║
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
    _remove_json_mcp(Path.home() / ".cursor" / "mcp.json")
    _remove_json_mcp(_project_root / ".cursor" / "mcp.json")
    _remove_json_mcp(Path.home() / ".claude" / "settings.json")
    _remove_json_mcp(Path.home() / ".claude" / "mcp.json")
    unregister_claude_projects()
    unregister_codex(Path.home() / ".codex" / "config.toml")
    unregister_codex(_project_root / ".codex" / "config.toml")


def register_targets(targets: list[str], base_url: str, token: str, basic: str | None) -> None:
    mapping = {
        "cursor": register_cursor,
        "claude": register_claude_projects,
        "claude-settings": register_claude_settings,
        "claude-mcp": register_claude_mcp,
        "codex": register_codex,
        "project-cursor": register_project_cursor,
        "project-codex": register_project_codex,
    }
    for t in targets:
        fn = mapping.get(t)
        if fn:
            print(f"\n  → 写入 {t} ...")
            fn(base_url, token, basic)


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


def install_skills() -> None:
    """同步 skills 到项目 .cursor/skills，并生成 AGENTS.md 供 Codex/Claude 引用。"""
    dst_root = _project_root / ".cursor" / "skills"
    dst_root.mkdir(parents=True, exist_ok=True)

    skill_map = {
        "easyshell-mcp": _project_root / ".cursor" / "skills" / "easyshell-mcp" / "SKILL.md",
        "easyshell-real": _package_dir / "easyshell-mcp" / "easyshell-real" / "skill.md",
        "easyshell-lateral": _package_dir / "easyshell-mcp" / "easyshell-lateral" / "skill.md",
        "easyshell-phishing": _package_dir / "easyshell-mcp" / "easyshell-phishing" / "skill.md",
        "easyshell-payload": _package_dir / "easyshell-mcp" / "easyshell-payload" / "skill.md",
        "easyshell-pentest": _package_dir / "skill" / "easyshell-pentest.md",
    }

    for name, src in skill_map.items():
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

文档入口：`easyshell-package/README.md`
"""
    if marker not in content:
        agents_md.write_text((content.rstrip() + "\n\n" + block + "\n"), encoding="utf-8")
        print(f"  ✓ 已更新 {agents_md}")
    else:
        # 更新已有段落（追加新 skill 行等）
        import re
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


async def main_async(args):
    if args.install_skills:
        print("\n  → 安装 Skills ...")
        install_skills()
        return

    base_url = args.url or get_input("EasyShell 地址", "http://127.0.0.1:8080")
    username = args.username or get_input("用户名", "")
    password = args.password or get_input("密码", "")

    if args.uninstall:
        unregister_all()
        print("\n  [OK] 卸载完成，请重启各 IDE/CLI 后生效")
        return

    token, basic, api = await login_and_verify(base_url, username, password, use_basic=args.basic)
    if not token:
        sys.exit(1)

    clients_online = 0
    default_uid = ""
    if not args.no_test and api:
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

    save_local_config(base_url, token, basic, clients_online=clients_online, default_uid=default_uid)

    targets = parse_targets(args.target)
    if args.reinstall or args.write_project or args.write_all:
        if args.reinstall:
            unregister_all()
        register_targets(targets, base_url, token, basic)
        install_skills()

    print("  ──────────────────────────────────────────")
    print("  环境变量（手动配置时可复制）:")
    for k, v in mcp_env_block(base_url, token, basic).items():
        print(f"    set {k}={v}")
    if default_uid:
        print(f"    set EASYSHELL_UID={default_uid}")
    print()
    print("  已支持平台: Cursor / Claude Code / Codex")
    print("  Cursor: 设置 → MCP → 重启")
    print("  Claude: 重启后 /mcp 检查 easyshell_* 工具")
    print("  Codex: 重启 CLI 或 /mcp 检查")
    print("  Skills: python easyshell_config.py --install-skills")


def main():
    banner()
    parser = argparse.ArgumentParser(
        description="EasyShell MCP 多平台配置",
        epilog="示例:\n"
               "  python easyshell_config.py --reinstall\n"
               "  python easyshell_config.py --target cursor,claude,codex --password xxx\n"
               "  python easyshell_config.py --install-skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-u", "--url", help="TeamServer 地址")
    parser.add_argument("--username", help="用户名")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--basic", action="store_true", help="登录时附带 HTTP Basic")
    parser.add_argument("--target", default="all", help=f"写入目标: all 或逗号分隔 {','.join(ALL_TARGETS)}")
    parser.add_argument("--write-project", action="store_true", help="写入项目级与全部平台（同 --target all）")
    parser.add_argument("--write-all", action="store_true", help="写入全部平台")
    parser.add_argument("--reinstall", action="store_true", help="卸载后重装全部平台")
    parser.add_argument("--uninstall", action="store_true", help="从全部平台移除")
    parser.add_argument("--install-skills", action="store_true", help="仅安装/同步 skills 与 AGENTS.md")
    parser.add_argument("--no-test", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
