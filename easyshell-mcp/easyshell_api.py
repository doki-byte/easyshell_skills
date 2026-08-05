"""EasyShell TeamServer 控制台 API 客户端（AES-GCM + RSA-OAEP，与 Web 前端一致）。"""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

API_PREFIX = "/prod-api/api"
AES_KEY_LEN = 32
GCM_IV_LEN = 12


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.strip())


class EasyShellAPI:
    """封装 EasyShell 控制台 REST API（含载荷加解密）。"""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        basic_auth: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.basic_auth = basic_auth
        self.timeout = timeout
        self._public_key = None

    @property
    def api_root(self) -> str:
        return f"{self.base_url}{API_PREFIX}"

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization2"] = f"Bearer {self.token}"
        if self.basic_auth:
            headers["Authorization"] = self.basic_auth
        return headers

    async def fetch_public_key(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.api_root}/crypto/public-key")
            r.raise_for_status()
            data = r.json()
        pem = (data.get("data") or {}).get("public_key", "").strip()
        if not pem:
            raise RuntimeError("无法获取 API RSA 公钥")
        self._public_key = serialization.load_pem_public_key(pem.encode("utf-8"))

    async def ensure_public_key(self) -> None:
        if self._public_key is None:
            await self.fetch_public_key()

    def _rsa_encrypt(self, data: bytes) -> str:
        if self._public_key is None:
            raise RuntimeError("RSA 公钥未加载")
        enc = self._public_key.encrypt(
            data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return _b64(enc)

    def _aes_encrypt(self, key: bytes, iv: bytes, plain: bytes) -> str:
        return _b64(AESGCM(key).encrypt(iv, plain, None))

    def _aes_decrypt(self, key: bytes, iv_b64: str, payload_b64: str) -> bytes:
        iv = _b64d(iv_b64)
        payload = _b64d(payload_b64)
        return AESGCM(key).decrypt(iv, payload, None)

    def _encrypt_body(self, body: Any) -> tuple[dict[str, Any], bytes]:
        key = secrets.token_bytes(AES_KEY_LEN)
        iv = secrets.token_bytes(GCM_IV_LEN)
        plain = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        return {
            "encrypted": True,
            "key": self._rsa_encrypt(key),
            "iv": self._rsa_encrypt(iv),
            "payload": self._aes_encrypt(key, iv, plain),
        }, key

    def _build_get_headers(self) -> tuple[dict[str, str], bytes]:
        key = secrets.token_bytes(AES_KEY_LEN)
        iv = secrets.token_bytes(GCM_IV_LEN)
        headers = {
            "X-Encrypted": "1",
            "X-Enc-Key": self._rsa_encrypt(key),
            "X-Enc-IV": self._rsa_encrypt(iv),
        }
        return headers, key

    def _encrypt_query_params(self, aes_key: bytes, params: dict[str, Any]) -> str:
        iv = secrets.token_bytes(GCM_IV_LEN)
        plain = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        envelope = {
            "encrypted": True,
            "iv": _b64(iv),
            "payload": self._aes_encrypt(aes_key, iv, plain),
        }
        return _b64(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))

    def _decrypt_response(self, resp_json: Any, aes_key: bytes) -> Any:
        if isinstance(resp_json, dict) and resp_json.get("encrypted") and resp_json.get("payload"):
            plain = self._aes_decrypt(aes_key, resp_json["iv"], resp_json["payload"])
            return json.loads(plain.decode("utf-8"))
        return resp_json

    async def login(self, username: str, password: str) -> str:
        await self.ensure_public_key()
        body = {"username": username, "password": password}
        envelope, aes_key = self._encrypt_body(body)
        headers = {"Content-Type": "application/json"}
        if self.basic_auth:
            headers["Authorization"] = self.basic_auth
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.api_root}/users/login",
                headers=headers,
                json=envelope,
            )
            r.raise_for_status()
            data = self._decrypt_response(r.json(), aes_key)
        token = (data.get("data") or {}).get("token")
        if not token:
            raise RuntimeError(f"登录失败: {data}")
        self.token = token
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        skip_encrypt: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        await self.ensure_public_key()
        method = method.upper()
        url = f"{self.api_root}/{path.lstrip('/')}"
        headers = self._auth_headers()
        aes_key: Optional[bytes] = None
        content: Any = None

        if skip_encrypt:
            if json_body is not None:
                headers["Content-Type"] = "application/json"
                content = json_body
        elif method in ("POST", "PUT", "PATCH") and json_body is not None:
            envelope, aes_key = self._encrypt_body(json_body)
            headers["Content-Type"] = "application/json"
            headers["X-Encrypted"] = "1"
            content = envelope
        elif method in ("GET", "DELETE", "POST"):
            enc_headers, aes_key = self._build_get_headers()
            headers.update(enc_headers)
            if params:
                headers["X-Enc-Params"] = self._encrypt_query_params(aes_key, params)
                params = None

        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            r = await client.request(method, url, headers=headers, json=content, params=params)
            r.raise_for_status()
            if not r.content:
                return {}
            try:
                resp_json = r.json()
            except json.JSONDecodeError:
                return r.content
        if aes_key is not None:
            return self._decrypt_response(resp_json, aes_key)
        return resp_json

    async def get(self, path: str, params: Optional[dict[str, Any]] = None, **kw: Any) -> Any:
        return await self.request("GET", path, params=params, **kw)

    async def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json_body=json_body, **kw)

    async def upload_multipart(
        self,
        path: str,
        *,
        fields: dict[str, str],
        file_field: str,
        file_content: bytes,
        filename: str,
        timeout: Optional[float] = None,
    ) -> Any:
        """multipart 上传（与 Web 前端一致，跳过 AES 加密）。"""
        await self.ensure_public_key()
        url = f"{self.api_root}/{path.lstrip('/')}"
        headers = self._auth_headers()
        files = {file_field: (filename, file_content)}
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            r = await client.post(url, headers=headers, data=fields, files=files)
            r.raise_for_status()
            if not r.content:
                return {}
            try:
                return r.json()
            except json.JSONDecodeError:
                return r.content

    async def download_binary(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        """下载二进制响应（GenServer/shellcode 等；请求体仍走 AES 加密）。"""
        await self.ensure_public_key()
        url = f"{self.api_root}/{path.lstrip('/')}"
        headers = self._auth_headers()
        aes_key: Optional[bytes] = None
        content: Any = None
        method = "POST" if json_body is not None else "GET"

        if method == "POST" and json_body is not None:
            envelope, aes_key = self._encrypt_body(json_body)
            headers["Content-Type"] = "application/json"
            headers["X-Encrypted"] = "1"
            content = envelope
        elif method == "GET":
            enc_headers, aes_key = self._build_get_headers()
            headers.update(enc_headers)
            if params:
                headers["X-Enc-Params"] = self._encrypt_query_params(aes_key, params)
                params = None

        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            r = await client.request(method, url, headers=headers, json=content, params=params)
            r.raise_for_status()
            ct = (r.headers.get("content-type") or "").lower()
            if "application/json" in ct or (r.content and r.content[:1] == b"{"):
                try:
                    resp_json = r.json()
                    if aes_key is not None:
                        resp_json = self._decrypt_response(resp_json, aes_key)
                    if isinstance(resp_json, dict):
                        err = resp_json.get("error") or resp_json.get("message")
                        data = resp_json.get("data")
                        if resp_json.get("status") not in (200, None) or err:
                            raise RuntimeError(str(err or data or resp_json))
                except json.JSONDecodeError:
                    pass
            return r.content


def client_from_env() -> EasyShellAPI:
    base = os.environ.get("EASYSHELL_URL", "http://127.0.0.1:8080")
    token = os.environ.get("EASYSHELL_TOKEN")
    basic = os.environ.get("EASYSHELL_BASICAUTH")
    return EasyShellAPI(base, token=token, basic_auth=basic)
