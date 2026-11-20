"""临时 AV 实战测试 CLI：封装 EasyShell 控制台 API（列客户端/执行命令/上传/读文件）。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from easyshell_api import EasyShellAPI  # noqa: E402

URL = "http://192.168.42.1:58090"
TOKEN = os.environ.get("ET_TOKEN") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VybmFtZSI6Im11aGFuIiwiZXhwIjoxNzg4Njg4MTIyfQ.3NY4fK7q7LMoRPmqJPghn8R5coOtdjh8vHvUZmU99Zo"
BASIC = "Basic bXVoYW46bXVoYW4="


def _ok(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False
    return resp.get("status") in (200, None) and not resp.get("error")


def _err(resp) -> str:
    if isinstance(resp, dict):
        return str(resp.get("error") or resp.get("message") or resp.get("data") or resp)
    return str(resp)


async def list_clients(api):
    resp = await api.get("client/clientslist", params={"page": 1, "page_size": 9999})
    if not _ok(resp):
        print("查询失败:", _err(resp))
        return
    items = (resp.get("data") or {}).get("list") or []
    print(f"共 {len(items)} 条:")
    for c in items:
        print(
            "  %s %-34s | %-16s | %-15s | %-16s | %-12s | %s"
            % (
                "+" if str(c.get("Online", "")) != "" else "-",
                str(c.get("Uid", "?")),
                str(c.get("Computer", "?"))[:16],
                str(c.get("InternalIP", "?"))[:15],
                str(c.get("ExternalIP", "?"))[:16],
                str(c.get("Username", "?"))[:12],
                str(c.get("Listener", "?")),
            )
        )


async def run_cmd(api, uid, command, timeout=60):
    """下发命令，轮询直到输出稳定（连续两次无新增）后返回完整增量。"""
    import time as _t

    before = await api.get("client/shell/getshellcontent", params={"uid": uid, "_t": _t.time() * 1000})
    base = ((before.get("data") or "") if _ok(before) else "") or ""
    resp = await api.post("client/shell/sendcommand", json_body={"uid": uid, "command": command})
    if resp.get("status") not in (200, None) and resp.get("error"):
        print("下发失败:", _err(resp))
        return
    deadline = _t.time() + min(timeout, 120)
    stable_since = None
    last = base
    while _t.time() < deadline:
        await asyncio.sleep(1.5)
        try:
            cur_resp = await api.get(
                "client/shell/getshellcontent", params={"uid": uid, "_t": _t.time() * 1000}, timeout=30
            )
        except Exception:
            continue
        if not _ok(cur_resp):
            continue
        cur = cur_resp.get("data") or ""
        if cur == last:
            if stable_since is None:
                stable_since = _t.time()
            elif _t.time() - stable_since >= 3:
                break  # 输出已稳定
            continue
        stable_since = _t.time()
        last = cur
    out = ""
    if len(last) > len(base):
        out = last[len(base):].strip() if last.startswith(base) else last.strip()
    if not out:
        final = await api.get("client/shell/getshellcontent", params={"uid": uid, "_t": _t.time() * 1000})
        if _ok(final):
            cur = final.get("data") or ""
            out = cur[len(base):].strip() if cur.startswith(base) else cur.strip()
    print(out[:12000] if out else "(无新输出)")


async def upload_file(api, uid, local_path, remote_path, timeout=600):
    import time
    content = open(local_path, "rb").read()
    resp = await api.upload_multipart(
        "client/file/upload",
        fields={"uid": uid, "uploadPath": remote_path},
        file_field="file",
        file_content=content,
        filename=os.path.basename(local_path),
        timeout=timeout,
    )
    if not _ok(resp):
        print("上传失败:", _err(resp))
        return
    data = resp.get("data") or {}
    upload_id = str(data.get("uploadId") or "")
    print(f"已入队: {os.path.basename(local_path)} ({data.get('totalBytes')} B) uploadId={upload_id}")
    if not upload_id:
        print("警告: 无 uploadId")
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2)
        info = await api.get("client/uploads/info", params={"uid": uid})
        if _ok(info):
            for job in info.get("data") or []:
                if str(job.get("uploadId", "")) != upload_id:
                    continue
                st = str(job.get("status") or "")
                if st == "done":
                    print("上传完成 ✓")
                    return
                if st == "error":
                    print("上传错误:", job.get("error"))
                    return
    print("上传超时")


async def read_file(api, uid, path, max_bytes=204800, timeout=60):
    """读文件，失败自动重试一次（filecontent 偶发慢响应）。"""
    for attempt in range(2):
        try:
            resp = await api.post(
                "client/file/filecontent",
                json_body={"uid": uid, "path": path},
                timeout=timeout,
            )
            break
        except Exception as e:
            print(f"(read 重试{attempt + 1}: {e})")
            await asyncio.sleep(2)
            resp = None
    if resp is None:
        print("读取失败(两次超时)")
        return
    if not _ok(resp):
        print("读取失败:", _err(resp))
        return
    content = resp.get("content") or ""
    raw = content.encode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        print(raw[:max_bytes].decode("utf-8", errors="replace") + f"\n...(截断 {len(raw)} B)")
    else:
        print(content if content else "(空文件)")


async def list_dir(api, uid, path):
    resp = await api.post(
        "client/file/tree",
        json_body={"uid": uid, "dirPath": path, "forceRefresh": True},
        timeout=130,
    )
    if not _ok(resp):
        print("列出失败:", _err(resp))
        return
    data = resp.get("data")
    items = []
    if isinstance(data, dict):
        items = data.get("entries") or data.get("items") or data.get("children") or []
    elif isinstance(data, list):
        items = data
    print(f"{path} ({len(items)} 项):")
    for f in items[:200]:
        if isinstance(f, dict):
            name = f.get("name") or f.get("Name") or "?"
            typ = str(f.get("type") or f.get("Type") or "").upper()
            is_dir = typ == "D" or f.get("isDir") or f.get("IsDir")
            size = f.get("size") or f.get("Size") or 0
            print(f"  [{'D' if is_dir else 'F'}] {name}  {size}")
        else:
            print(f"  {f}")


async def dlreq(api, uid, path):
    resp = await api.post("client/file/download", json_body={"uid": uid.strip(), "filePath": path})
    if _ok(resp):
        print("已下发下载:", path)
    else:
        print("下发失败:", _err(resp))


async def dllist(api, uid):
    params = {"uid": uid.strip()} if uid.strip() else None
    resp = await api.get("client/downloads/info", params=params)
    if not _ok(resp):
        print("查询失败:", _err(resp))
        return
    items = resp.get("data") or []
    if not items:
        print("无下载任务")
        return
    for d in items[:20]:
        print(
            "%-32s | %-40s | %s | %s/%s"
            % (
                str(d.get("Uid", "?"))[:32],
                str(d.get("FilePath", "?"))[:40],
                str(d.get("DownloadState", d.get("QueueState", "?")))[:12],
                d.get("DownloadedSize") or 0,
                d.get("FileSize") or 0,
            )
        )


async def dlpull(api, uid, remote, local):
    import httpx

    last = None
    for key in ("filePath", "path", "fileName"):
        try:
            data = await api.download_binary(
                "client/downloads/downloaded_file",
                json_body={"uid": uid.strip(), key: remote},
                timeout=600,
            )
            if not data:
                print("空文件或下载未完成")
                return
            with open(local, "wb") as f:
                f.write(data)
            print(f"已保存 {len(data)} bytes -> {local}")
            return
        except httpx.HTTPStatusError as e:
            last = f"HTTP {e.response.status_code} {e.response.text[:300]}"
        except Exception as e:
            last = repr(e)
    print("拉取失败:", last)


async def shot(api, uid, local, quality=78, max_edge=1440):
    import base64 as _b64
    resp = await api.get(
        "client/screen/capture",
        params={"uid": uid, "quality": quality, "maxEdge": max_edge},
        timeout=300,
    )
    if not _ok(resp):
        print("截屏失败:", _err(resp))
        return
    data = resp.get("data") or {}
    b64 = str(data.get("imageBase64") or "").strip()
    if not b64 or b64.startswith("{") or b64.startswith("["):
        print("截屏失败: 无有效图像")
        return
    raw = _b64.b64decode(b64)
    ext = ".png"
    if raw[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    if local.lower().endswith(".png") or local.lower().endswith(".jpg"):
        p = local
    else:
        p = local + ext
    with open(p, "wb") as f:
        f.write(raw)
    print(f"截屏已保存 {len(raw)} bytes -> {p}")


async def main():
    api = EasyShellAPI(URL, token=TOKEN, basic_auth=BASIC, timeout=60)
    if len(sys.argv) < 2:
        print("用法: _avtest_cli.py login|clients|exec <uid> <cmd>|upload <uid> <本地> <远端>|read <uid> <path>")
        return
    cmd = sys.argv[1]
    if cmd == "login":
        tok = await api.login("muhan", "muhan")
        print("TOKEN=" + tok)
        return
    if cmd == "clients":
        await list_clients(api)
    elif cmd == "ls" and len(sys.argv) >= 4:
        await list_dir(api, sys.argv[2], sys.argv[3])
    elif cmd == "exec" and len(sys.argv) >= 4:
        await run_cmd(api, sys.argv[2], sys.argv[3])
    elif cmd == "upload" and len(sys.argv) >= 5:
        await upload_file(api, sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "shot" and len(sys.argv) >= 4:
        await shot(api, sys.argv[2], sys.argv[3])
    elif cmd == "dlreq" and len(sys.argv) >= 4:
        await dlreq(api, sys.argv[2], sys.argv[3])
    elif cmd == "dllist" and len(sys.argv) >= 3:
        await dllist(api, sys.argv[2])
    elif cmd == "dlpull" and len(sys.argv) >= 5:
        await dlpull(api, sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "read" and len(sys.argv) >= 4:
        await read_file(api, sys.argv[2], sys.argv[3])
    else:
        print("参数错误")


asyncio.run(main())
