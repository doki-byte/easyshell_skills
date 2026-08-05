# EasyShell MCP 实战复盘

## 掉线常见原因

1. **连发多条 exec_cmd** — 每条开一个 cmd.exe，EDR 报警后杀进程
2. **type 读大文件** — 额外 cmd 进程 + 输出撑爆 shell 缓冲区
3. **隧道长期不关** — 异常流量持续，易被检测
4. **fscan 反复落盘** — 应用层 AV 扫到 exe

## 猥琐发育原则

- 一次只发 1 个 API，等返回再发下一个
- 命令合并：`cmd /c "whoami&ipconfig&netstat -ano"`
- 读文件用 `easyshell_read_file`，扫描用 `inline-bin`
- 隧道用完 `easyshell_stop_socks5`
- HTTP Beacon sleep 大时先 `easyshell_set_sleep(uid, 3)`

## SOCKS5 教训

- 端口在 TeamServer，**出口 IP = 植入体**
- 本机能直连内网时，优先 `easyshell_send_http`，不必绕隧道
- ping 通 ≠ TCP 通

详见 `memory/easyshell-tunnel-lessons.md`。
