# EasyShell MCP 接入 DSH（DeepSeek Harness）踩坑记录

## 为什么「别的工具装完，DSH 里还是找不见工具」

DSH 不读 `mcp.json` / `config.toml`。它的插件树由 **patch 层**组合而成，顺序是：

```
bundle 层（dsh-base、dsh-tui…）
  → profile 自己的 cordis.patch.yml
  → home 级 $DSH_HOME/cordis.patch.yml     ← 装这里，全 profile 生效
  → --patch 覆盖层
  → telemetry 开关
```

MCP server 由 `@deepseek-ai/dsh-mcp-client` 挂载，**每条 server 一行配置**；
模型看到的工具名固定为 `mcp__<serverName>__<rawName>`，所以 EasyShell 是
`mcp__easyshell__easyshell_exec_cmd`。

## 三个必踩的坑

| 坑 | 现象 | 正解 |
|----|------|------|
| 插入语法 | 直接写 `- id: mcp-easyshell` 只报 `patch: entry "mcp-easyshell" not found`（缺省 patch 只做覆盖/禁用） | 新增插件必须写 `- insert:` + 子项列表 |
| 两次写入 | 先清空再写回，live watcher 把两次写入合并成一次 → 条目被删了没加回来，表现就是「工具又不见了」 | 装机时**只写一次**：原地替换自己那一行，别先 uninstall 再 register |
| 空文件 | 空文件或纯注释的 patch 文件会让 dsh **启动失败** | 停用时写 `[]`，不要删文件、不要只留注释 |

## 环境变量会被清洗

stdio 子进程的环境 = `scrubbedParentEnv()`（删掉 `/KEY|PASSWORD|SECRET|TOKEN/i` 与全部 `DSH_*`）
再合并 `config.env`。所以：

- Token **必须**显式写在 `config.env` 里，不能指望继承 `EASYSHELL_TOKEN`；
- `command` 用**绝对解释器路径**（`sys.executable`），别依赖 PATH。

## Skills 放哪

DSH 扫的根目录（rank 越小越优先）：

| Rank | 来源 | 路径 |
|------|------|------|
| 100 | project-dsh | `<项目根>/.dsh/skills` |
| 200 | project-agents | `<项目根>/.agents/skills` |
| 400 | user-dsh | `$DSH_HOME/skills` ← 装机用这个 |
| 500 | user-agents | `~/.agents/skills` |

格式是 `<名>/SKILL.md` bundle，frontmatter **必须**有 `name` + `description`
（缺了会被静默忽略，只在日志里 warn）。本仓库 6 个 skill 的源文件都已带 frontmatter。

## 验证命令（三连）

```powershell
# 1) patch 行在不在组合结果里
dsh --profile dsh-tui --dump-config | Select-String easyshell

# 2) 插件有没有真加载（会常驻一个 python server.py 子进程）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'server\.py' } | Select-Object ProcessId

# 3) 让模型直接调：mcp__easyshell__easyshell_diagnose
```

三者都对 = 装好了。`patchReload: live` 的 profile（dsh-tui 默认）改完免重启，几秒内热重载。

## 参考

- `easyshell-mcp/easyshell_config.py` → `register_dsh` / `verify_dsh` / `sync_dsh_skills`
- `@deepseek-ai/dsh-mcp-client` README（配置字段、重连策略）
- `@deepseek-ai/dsh-app-boot` README.zh.md（patch 层语义）
