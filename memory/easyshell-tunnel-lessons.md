# EasyShell SOCKS5 隧道方法论

## 架构

```
操作员本机 ──→ TeamServer:PORT ──→ 植入体 UID ──→ 内网目标
                  ↑ 监听              ↑ 真实出口 IP
```

**SOCKS5 端口绑在 TeamServer，TCP 从植入体所在网络发出。**

## 判断流程

1. `ipconfig` / 看本机是否有 VMnet、物理网卡直达目标网段
2. 能直连 → `curl http://目标/` 直接打
3. 不能直连 → `easyshell_start_socks5(uid, force=True)`
4. 建隧道后：`curl --socks5 127.0.0.1:PORT --max-time 5 http://目标/`

## 常见误判

| 现象 | 原因 |
|------|------|
| ping 通、curl 超时 | 防火墙拦 TCP 不拦 ICMP |
| 换 UID 后隧道失效 | 新植入体 IP/网段不同 |
| 隧道通但扫不到 | Agent 与目标不在同一路由域 |

## EasyShell API

```json
POST /prod-api/api/socks5/start
{
  "uid": "<植入体UID>",
  "Socks5port": "1080",
  "UserName": "",
  "Password": "",
  "Type": "socks5"
}
```

Type 还可为 `tcp` / `udp`（需 `TargetAddr` 固定转发）。

## 参考

- easyshell_server/internal/httpapi/socks5.go
- easyshell_server/internal/proxy/
