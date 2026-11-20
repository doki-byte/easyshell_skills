# EasyShell MCP — 最简单使用指南

把 EasyShell TeamServer 接入 Cursor / Claude Code / Codex / DSH，在对话里直接控制上线植入体。

---

## 前置条件

| 项目 | 说明 |
|------|------|
| TeamServer | 已启动，浏览器能打开 `http://你的IP:8080` |
| 账号 | 默认 `admin`，密码首次运行时在控制台或 `logs/` 中查看 |
| Python | 3.10+，已安装 `pip` |
| 客户端 | Cursor / Claude Code / Codex CLI / DSH 等支持 MCP 的工具 |

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
| **DSH** | `$DSH_HOME/cordis.patch.yml`（home 级 patch，全 profile 生效）+ `$DSH_HOME/skills/` |
| **Skills** | `.cursor/skills/` + 项目 `AGENTS.md` |

只装部分平台：

```powershell
python easyshell_config.py --url http://127.0.0.1:8080 --password <密码> --target cursor,claude,codex,dsh
```

只装 DSH（TeamServer 暂时连不上也能装，复用上次 Token）：

```powershell
python easyshell_config.py --target dsh
python easyshell_config.py --target dsh --from-config
```

仅同步 Skills（Cursor + DSH 一起）：`python easyshell_config.py --install-skills`

卸载全部：`python easyshell_config.py --uninstall`

---

## DSH（DeepSeek Harness）一键安装

DSH 的 MCP 不读 `mcp.json`，而是读 profile 的 patch 层，所以别的工具装完 DSH 依然「找不见工具」。
本工具会把一条 `mcp-easyshell` 行写进 **home 级 patch** `$DSH_HOME/cordis.patch.yml`，
对本机所有 profile（dsh-tui / web / desktop …）一次生效：

```yaml
- insert:
    - id: mcp-easyshell
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: easyshell
        transport: stdio
        command: 'E:\Python\Python313\python.exe'
        args: ['E:/.../easyshell-mcp/server.py']
        env: { EASYSHELL_URL: '...', EASYSHELL_TOKEN: '...' }
```

| 行为 | 说明 |
|------|------|
| 工具名 | `mcp__easyshell__easyshell_exec_cmd`（DSH 固定 `mcp__<server>__<tool>` 命名） |
| 生效 | `patchReload: live` 的 profile（dsh-tui 等）**热重载，装完即用，不用重启** |
| 校验 | 安装后自动跑 `dsh --profile dsh-tui --dump-config`，确认 patch 真被组合（`--no-dsh-verify` 可跳过） |
| 自定义位置 | `--dsh-home <路径>`（默认 `$DSH_HOME`，否则 `~/.dsh`） |
| 幂等 | 重复安装只保留一份；文件里别人的 patch 行不会被删 |
| 卸载 | `--uninstall` 或 `--target dsh` + 手工删块；文件会留 `[]`（**空文件会让 dsh 启动失败**，故不删文件） |
| Skills | 同一步把 6 个 skill 装到 `$DSH_HOME/skills/<名>/SKILL.md`，会话里直接可见 |

避坑：

- 别手改 `$DSH_HOME/profiles/*/cordis.yml`（每次启动都会被重写），要改就改 `cordis.patch.yml`。
- patch 里新增插件**必须**写成 `- insert:` 列表；直接写 `- id: xxx` 只会被当成「覆盖已存在条目」并报 `entry not found`。
- DSH 会清洗 MCP 子进程环境（丢掉 `*TOKEN*`/`*KEY*`/`DSH_*`），所以 Token 是显式写在 `config.env` 里的，
  不是靠继承环境变量。
- 改 DSH 相关代码后跑一次回归自测（离线、不碰真实 `$DSH_HOME`）：
  `python easyshell-mcp\tests\test_dsh_patch.py`

---

## 各平台验证

| 平台 | 验证方式 |
|------|----------|
| Cursor | 设置 → MCP → `easyshell` 绿色 |
| Claude Code | 重启后输入 `/mcp`，应有 `easyshell_*` 工具 |
| Codex | 重启 CLI，输入 `/mcp` 或问模型能否调用 easyshell 工具 |
| DSH | 直接问「列出所有在线植入体」；看工具名应是 `mcp__easyshell__easyshell_list_clients`。没出现就重跑 `--target dsh`，它自带 `--dump-config` 校验 |

DSH 排障一句话版：**工具名以 `mcp__easyshell__` 开头＝装好了**；只有 `easyshell_*` 一个都看不见，说明 patch 没被组合，跑
`dsh --profile dsh-tui --dump-config | Select-String easyshell` 看条目在不在。

---

## 开始使用

安装完成后，在对话里直接用自然语言，例如：

```
帮我诊断 EasyShell 连接
列出所有在线植入体
对 uid=xxx 执行 whoami 和 ipconfig，合并成一条命令
```

Agent 会自动调用 `easyshell_*` MCP 工具（DSH 里是 `mcp__easyshell__easyshell_*`，用法一致）。

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

## 截屏与对话展示

单次截屏走同步 REST（实时键鼠/录屏仍是 Web WebSocket 流），截图会保存为**本机图片**，
在对话里直接打开即可看到并让模型分析画面：

```
easyshell_screen_capture(uid)           # ① 截屏：图片落 easyshell_screenshots/ + 归档服务端历史
                                        # ② 用图像查看能力打开返回的本地路径 → 截图出现在对话中
easyshell_screen_history(uid)           # ③ 换会话后回看：列出归档
easyshell_screen_history_get(id)        #    下载归档图到本机
easyshell_screen_history_delete(id)     #    清理归档
```

- 默认保存目录：MCP 工作目录下 `easyshell_screenshots/`，可用环境变量 `EASYSHELL_SCREENSHOT_DIR` 修改
- 慢 Beacon（http/dns、sleep 大）先 `easyshell_set_sleep(uid, 3)` 再截，必要时加大 `timeout`（上限 300s）
- 无桌面会话的 Windows 宿主会截屏失败（Shell 中 `!Error` 有原因），非工具问题

---

## 加载 Skills

`--reinstall` 已自动同步到 `.cursor/skills/`（Cursor）与 `$DSH_HOME/skills/`（DSH）。也可手动 `@` 引用：

| Skill | 仓库内权威源 |
|-------|------|
| easyshell-mcp | `easyshell-mcp/easyshell-mcp/skill.md` |
| easyshell-real | `easyshell-mcp/easyshell-real/skill.md` |
| easyshell-lateral | `easyshell-mcp/easyshell-lateral/skill.md` |
| easyshell-phishing | `easyshell-mcp/easyshell-phishing/skill.md` |
| easyshell-payload | `easyshell-mcp/easyshell-payload/skill.md` |
| easyshell-pentest | `skill/easyshell-pentest.md` |

Codex / Claude / DSH 也会读取项目根目录 `AGENTS.md` 中的技能索引；
DSH 的 skill 是 `<dshHome>/skills/<名>/SKILL.md` bundle（frontmatter 必须含 `name` + `description`，本仓库已就绪）。

---

## 目录结构

```
easyshell-package/
├── README.md
├── easyshell-mcp/
│   ├── server.py
│   ├── easyshell_api.py
│   ├── easyshell_config.py      # 多平台一键配置（含 DSH patch 层）
│   ├── pentest_common.py        # 脚本公共 API
│   ├── pentest/                 # 一次性渗透脚本（74 个）
│   ├── easyshell-mcp/           # MCP 工具与 API 映射 skill
│   ├── easyshell-real/
│   ├── easyshell-lateral/
│   ├── easyshell-phishing/
│   └── easyshell-payload/       # 载荷生成与 Web 投递
├── skill/easyshell-pentest.md
├── memory/
└── easyshell_screenshots/   # MCP 截屏落盘（运行时生成于 MCP 工作目录；可用 EASYSHELL_SCREENSHOT_DIR 指定）
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
- DSH 接入踩坑：`memory/easyshell-dsh-lessons.md`
- 实战复盘：`memory/easyshell-ops-lessons.md`
