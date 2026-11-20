#!/usr/bin/env python3
"""DSH patch 层写入逻辑自测（离线，不碰真实 $DSH_HOME）。

覆盖：
  1. YAML 合并/幂等/卸载 —— 不懂 YAML 的按行手术刀是否稳
  2. 临时 DSH home 上的注册→卸载闭环、skills 安装与清理
  3. patch 块结构（dsh 只认 `- insert:` 这一种新增语法）

跑法：
  python tests\\test_dsh_patch.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "easyshell_config.py"
spec = importlib.util.spec_from_file_location("easyshell_config", SRC)
ecfg = importlib.util.module_from_spec(spec)
sys.modules["easyshell_config"] = ecfg
spec.loader.exec_module(ecfg)

URL = "http://127.0.0.1:8080"
TOKEN = "aaa.bbb.ccc"
BASIC = "Basic eDp5"
BLOCK = ecfg.dsh_patch_block(URL, TOKEN, BASIC)
USER_PATCH = "# 用户自己的 patch\n- id: some-other-plugin\n  config:\n    enabled: false\n"

_failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"   {extra}"))
    if not cond:
        _failed.append(name)


print("── 1. 逐行合并语义")
check("空文件渲染成纯 []", ecfg._dsh_patch_render("").strip() == ecfg.DSH_EMPTY_COMMENT + "\n[]")
check("[] 被识别为无条目", not ecfg._dsh_patch_has_entries(ecfg._strip_dsh_patch("[]\n")))
stripped = ecfg._prune_empty_inserts(ecfg._strip_dsh_patch(USER_PATCH))
check("用户条目保留", "- id: some-other-plugin" in stripped and "enabled: false" in stripped)

once = USER_PATCH + BLOCK
twice = ecfg._prune_empty_inserts(ecfg._strip_dsh_patch(once)) + BLOCK
check("重复安装幂等", twice.count("id: mcp-easyshell") == 1)
check("用户条目仍在", "- id: some-other-plugin" in twice)
check("缩进子块整体删除", twice.count("serverName:") == 1)

removed = ecfg._strip_dsh_patch(twice)
check("卸载只删自己", "mcp-easyshell" not in removed and "some-other-plugin" in removed)
orphan = ecfg._dsh_patch_render(ecfg._prune_empty_inserts(ecfg._strip_dsh_patch(BLOCK)))
check("空壳 insert 退化成 []", "insert:" not in orphan and not ecfg._dsh_patch_has_entries(orphan))
check("YAML 标量转义", ecfg._yq("a'b:c#d\\e") == "'a''b:c#d\\e'")

print("── 2. patch 块结构（新增插件必须用 insert）")
check("以 - insert: 开头", BLOCK.startswith("- insert:\n"))
check("行 id 正确", f"    - id: {ecfg.DSH_ROW_ID}" in BLOCK)
check("serverName 正确", f"serverName: {ecfg.DSH_SERVER_NAME}" in BLOCK)
check("Token 显式写进 env", f"EASYSHELL_TOKEN: '{TOKEN}'" in BLOCK)
check("command 是绝对路径", "command: '" in BLOCK and "python" in BLOCK.lower())
check("cwd 指向 server 目录", "cwd: '" in BLOCK)
check("超时放宽到 5 分钟", "toolCallTimeoutMs: 300000" in BLOCK)
check("启动失败不阻塞 harness", "failOnStartupError: false" in BLOCK)

try:
    import yaml  # noqa: PLC0415 —— 可选，装了就用真解析器再验一遍

    doc = yaml.safe_load(USER_PATCH + BLOCK)
    row = doc[1]["insert"][0]
    check("yaml 可解析且 insert 是 list", isinstance(doc, list) and isinstance(doc[1]["insert"], list))
    check("config.transport = stdio", row["config"]["transport"] == "stdio")
    check(
        "env 三件套齐全",
        {"EASYSHELL_URL", "EASYSHELL_TOKEN", "EASYSHELL_BASICAUTH"} == set(row["config"]["env"]),
    )
except ImportError:
    print("  (跳过 pyyaml 真解析：未安装)")

print("── 3. 临时 DSH home 闭环")
with tempfile.TemporaryDirectory(prefix="dsh-home-") as tmpdir:
    home = Path(tmpdir)
    patch = ecfg.dsh_patch_path(home)
    skills = ecfg.dsh_skill_root(home)

    check("首次安装成功", ecfg.register_dsh(URL, TOKEN, BASIC, home=home) is True)
    body = patch.read_text(encoding="utf-8")
    check("patch 文件已生成", patch.is_file() and "id: mcp-easyshell" in body)

    ecfg.register_dsh(URL, TOKEN, BASIC, home=home)
    check("二次安装内容稳定", patch.read_text(encoding="utf-8") == body)

    ecfg.register_dsh(URL, "new.token.x", BASIC, home=home)
    again = patch.read_text(encoding="utf-8")
    check("Token 轮换覆盖旧值", "new.token.x" in again and TOKEN not in again)

    names = ecfg.sync_dsh_skills(home)
    check("6 个 skill 落地", len(names) == 6)
    skill_md = skills / "easyshell-real" / "SKILL.md"
    check("SKILL.md 存在", skill_md.is_file())
    check(
        "frontmatter 含 name+description",
        skill_md.read_text(encoding="utf-8").startswith("---")
        and "name: easyshell-real" in skill_md.read_text(encoding="utf-8")
        and "description:" in skill_md.read_text(encoding="utf-8"),
    )
    check("托管标记存在", (skills / "easyshell-real" / ecfg.DSH_SKILL_MARKER).is_file())

    mine = skills / "my-own-skill"
    mine.mkdir(parents=True, exist_ok=True)
    (mine / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: x\n---\n", encoding="utf-8")

    check("卸载成功", ecfg.unregister_dsh(home=home) is True)
    after = patch.read_text(encoding="utf-8")
    check("我们的行已移除", "mcp-easyshell" not in after)
    check("留下合法空列表而非空文件", after.strip().endswith("[]") and not ecfg._dsh_patch_has_entries(after))
    check("托管 skill 已清理", not (skills / "easyshell-real").exists())
    check("别人的 skill 没被误删", (mine / "SKILL.md").is_file())

    patch.write_text(USER_PATCH + ecfg.dsh_patch_block(URL, TOKEN, BASIC), encoding="utf-8")
    ecfg.unregister_dsh(home=home)
    final = patch.read_text(encoding="utf-8")
    check("卸载保留用户 patch 行", "- id: some-other-plugin" in final and "a: 1" not in final)

print("\nRESULT:", "ALL PASS" if not _failed else f"FAILED: {_failed}")
sys.exit(0 if not _failed else 1)
