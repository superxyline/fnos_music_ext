"""
飞牛音乐（fnOS trim.music）API 客户端

逆向实现，参照以下开源第三方客户端交叉验证：
  - kuilei0926/FeiNiuMusic（Flutter，纯 Cookie 认证，2026-10 仍在更新）
  - Raduap/FN-Music-Apple（Electron，Cookie 认证）
  - qianlipp/fn-music-bridge 音桥（Go，Cookie + authx 签名，signer.go/config.go）
  - blog.zwbcc.cn fmusic（Kotlin，authx 签名详解）

接口要点：
  - 网关:  {base}/music/api/v1，响应统一 {code, msg, data}，code=120001 表示 token 失效
  - 登录:  POST /user/password-login  body={"username","password":sha256(明文),"deviceId"}
           登录请求不带 X-Music-API 头；其余请求都要带
  - 认证:  Cookie: music-token=<token>（+ authx 签名头，两种模式自动适配）
  - 收藏:  GET /favorite-track/list?sort=favoriteAt,desc&page=1&size=500
  - 流:    GET /track/stream?guid=<guid>  支持 Range，会 302 跳转
           （同源跳转保留认证，跨源跳转剥离全部认证头，防止凭据泄漏）
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import time
import urllib.parse

import httpx

log = logging.getLogger("fnmusic")

# authx 签名常量（写死在飞牛官方 Web 端 JS 中，见音桥 config.go defaultSigningPrefix/defaultClientKey）
AUTHX_PREFIX = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
AUTHX_KEY = "6D5602D4-A342-4799-A0F0-BB795E7167D0"

API_BASE = "/music/api/v1"


class FnMusicError(Exception):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _go_query_escape(value: str) -> str:
    """Go url.QueryEscape 语义：保留 A-Za-z0-9-_.~，空格转 %20（Go 先转 + 再替换）"""
    return urllib.parse.quote(value, safe="-_.~")


def sign_authx(method: str, pathname: str, query: dict | None, body: bytes,
               nonce: str | None = None, timestamp: str | None = None) -> str:
    """生成 authx 请求头（算法来自音桥 signer.go）

    sign = md5(prefix + "_" + pathname + "_" + nonce + "_" + timestamp + "_" + md5(payload) + "_" + key)
    GET 的 payload = 规范化 query（按 key 排序、Go 风格转义）再 URL-decode 的字节
    POST 的 payload = 请求体原文
    """
    nonce = nonce or f"{random.randint(100000, 999999)}"
    timestamp = timestamp or str(int(time.time() * 1000))
    if method.upper() == "GET":
        parts = []
        for key in sorted(query or {}):
            values = query[key]
            if not isinstance(values, (list, tuple)):
                values = [values]
            for v in values:
                parts.append(f"{_go_query_escape(key)}={_go_query_escape(str(v))}")
        encoded = "&".join(parts)
        try:
            payload = urllib.parse.unquote(encoded, errors="surrogatepass").encode(
                "utf-8", "surrogatepass"
            )
        except Exception:
            payload = encoded.encode("utf-8", "surrogatepass")
    else:
        payload = body or b""
    payload_hash = hashlib.md5(payload).hexdigest()
    source = f"{AUTHX_PREFIX}_{pathname}_{nonce}_{timestamp}_{payload_hash}_{AUTHX_KEY}"
    sign = hashlib.md5(source.encode("utf-8")).hexdigest()
    return f"nonce={nonce}&timestamp={timestamp}&sign={sign}"


def extract_token(data) -> str | None:
    """从登录响应中提取 token（兼容 token / userToken 等字段名）"""
    candidates = []
    if isinstance(data, dict):
        for key in ("token", "userToken", "user_token", "accessToken", "access_token"):
            if isinstance(data.get(key), str) and data[key]:
                candidates.append(data[key])
        for key in ("data", "result", "user"):
            sub = data.get(key)
            if isinstance(sub, dict):
                for k2 in ("token", "userToken", "user_token"):
                    if isinstance(sub.get(k2), str) and sub[k2]:
                        candidates.append(sub[k2])
    return candidates[0] if candidates else None


class FnMusicClient:
    """飞牛音乐 API 客户端（httpx 异步）"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        device_id: str | None = None,
        use_authx: bool = True,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.device_id = device_id or secrets.token_hex(16)
        self.use_authx = use_authx
        self.token: str | None = None
        self.token_obtained_at: float = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=300.0),
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "fnmusic-bridge/1.0"},
        )

    async def aclose(self):
        await self._client.aclose()

    # ---------- 基础请求 ----------
    def _api_headers(self, method: str, path: str, query: dict | None, body: bytes,
                     with_token: bool = True, with_version: bool = True) -> dict:
        headers = {"Accept": "application/json"}
        if with_version:
            headers["X-Music-API"] = "v1"
        if with_token and self.token:
            headers["Cookie"] = f"music-token={self.token}"
        if self.use_authx:
            headers["authx"] = sign_authx(method, path, query, body)
        return headers

    async def _raw_request(self, method: str, path: str, query: dict | None = None,
                           json_body: dict | None = None, with_token: bool = True,
                           with_version: bool = True, retry_on_auth: bool = True):
        url = self.base_url + API_BASE + path
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else b""
        headers = self._api_headers(method, path, query, body, with_token, with_version)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = await self._client.request(method, url, params=query, content=body or None,
                                          headers=headers)
        # token 失效：重登一次再重试
        if resp.status_code in (401, 403) and with_token and retry_on_auth:
            log.warning("请求 %s 返回 %s，尝试重新登录", path, resp.status_code)
            await self.login(force=True)
            return await self._raw_request(method, path, query, json_body, with_token,
                                           with_version, retry_on_auth=False)
        return resp

    async def api_call(self, method: str, path: str, query: dict | None = None,
                       json_body: dict | None = None, with_version: bool = True):
        """调用 API 并解析 {code,msg,data} 信封，返回 data"""
        resp = await self._raw_request(method, path, query, json_body, with_version=with_version)
        try:
            payload = resp.json()
        except Exception:
            raise FnMusicError(f"响应不是 JSON: HTTP {resp.status_code} {resp.text[:200]}",
                               status=resp.status_code)
        code = payload.get("code")
        if code == 120001:
            raise FnMusicError("token 失效（120001）", code=120001)
        if code != 0:
            raise FnMusicError(f"API 错误 code={code} msg={payload.get('msg')}", code=code,
                               status=resp.status_code)
        return payload.get("data")

    # ---------- 登录 ----------
    async def login(self, force: bool = False) -> str:
        """登录获取 token。自动适配 authx / 无 authx 两种模式。"""
        if self.token and not force:
            return self.token
        path = "/user/password-login"
        url = self.base_url + API_BASE + path
        body_dict = {
            "username": self.username,
            "password": sha256_hex(self.password),
            "deviceId": self.device_id,
        }
        body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        # 登录请求：不带 X-Music-API
        last_error = None
        for use_authx in (self.use_authx, not self.use_authx):
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if use_authx:
                headers["authx"] = sign_authx("POST", path, None, body)
            resp = await self._client.post(url, content=body, headers=headers)
            try:
                payload = resp.json()
            except Exception:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            if payload.get("code") == 0:
                token = extract_token(payload.get("data") or payload)
                if not token:
                    last_error = f"响应中未找到 token: {json.dumps(payload, ensure_ascii=False)[:300]}"
                    continue
                self.token = token
                self.token_obtained_at = time.time()
                self.use_authx = use_authx
                log.info("登录成功（authx=%s）", use_authx)
                return token
            last_error = f"code={payload.get('code')} msg={payload.get('msg')}"
        raise FnMusicError(f"登录失败: {last_error}")

    # ---------- 业务接口 ----------
    async def get_user_info(self):
        return await self.api_call("GET", "/user/me")

    async def search_track(self, keyword: str, page_size: int = 20) -> list[dict]:
        """搜索歌曲（fnmusicext 接管后该端点会聚合本地库与在线源）"""
        data = await self.api_call(
            "GET", "/search/track",
            {"q": keyword, "page": 1, "size": page_size},
        )
        return data.get("list") or []

    async def add_favorite(self, track_guid: str) -> None:
        """把歌曲加入飞牛音乐收藏"""
        await self.api_call("POST", "/favorite-track/create",
                            json_body={"trackGUID": track_guid})

    async def get_favorites(self, page_size: int = 500, max_pages: int = 40) -> list[dict]:
        """拉取全部收藏歌曲（分页）"""
        await self.login()
        tracks: list[dict] = []
        page = 1
        while page <= max_pages:
            data = await self.api_call(
                "GET", "/favorite-track/list",
                {"sort": "favoriteAt,desc", "page": page, "size": page_size},
            )
            items = data.get("list") or []
            tracks.extend(items)
            total = data.get("total")
            if not items or (isinstance(total, int) and len(tracks) >= total):
                break
            page += 1
        return tracks

    async def get_initialization_state(self):
        resp = await self._client.get(self.base_url + API_BASE + "/initialization/state",
                                      headers={"Accept": "application/json"})
        return resp.json()

    # ---------- 音频流 ----------
    async def open_stream(self, guid: str, range_header: str | None = None,
                          max_redirects: int = 5):
        """打开音频流，手动跟随 302（同源保留认证，跨源剥离），返回 httpx.Response

        调用方负责用 async with 关闭响应。token 失效时会自动重登重试一次。
        """
        await self.login()
        path = "/track/stream"
        query = {"guid": guid}
        url = self.base_url + API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "*/*",
            "X-Music-API": "v1",
            "Cookie": f"music-token={self.token}",
        }
        if self.use_authx:
            headers["authx"] = sign_authx("GET", path, query, b"")
        if range_header:
            headers["Range"] = range_header

        current_url = url
        current_headers = dict(headers)
        for _ in range(max_redirects + 1):
            resp = await self._client.send(
                self._client.build_request("GET", current_url, headers=current_headers),
                stream=True,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                await resp.aclose()
                if not location:
                    raise FnMusicError("302 跳转缺少 Location 头")
                next_url = urllib.parse.urljoin(str(resp.url), location)
                next_parsed = urllib.parse.urlparse(next_url)
                cur_parsed = urllib.parse.urlparse(current_url)
                same_origin = (
                    next_parsed.scheme == cur_parsed.scheme
                    and next_parsed.netloc == cur_parsed.netloc
                )
                if same_origin:
                    next_headers = dict(current_headers)
                else:
                    # 跨源：剥离全部认证信息，只保留通用头
                    next_headers = {"Accept": "*/*", "User-Agent": "fnmusic-bridge/1.0"}
                    if range_header:
                        next_headers["Range"] = range_header
                current_url, current_headers = next_url, next_headers
                continue
            if resp.status_code in (401, 403):
                await resp.aclose()
                log.warning("流请求返回 %s，重新登录后重试", resp.status_code)
                await self.login(force=True)
                headers = {
                    "Accept": "*/*",
                    "X-Music-API": "v1",
                    "Cookie": f"music-token={self.token}",
                }
                if self.use_authx:
                    headers["authx"] = sign_authx("GET", path, query, b"")
                if range_header:
                    headers["Range"] = range_header
                resp = await self._client.send(
                    self._client.build_request("GET", url, headers=headers), stream=True
                )
            return resp
        raise FnMusicError(f"重定向次数超过 {max_redirects}")
