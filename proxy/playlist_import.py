"""歌单导入引擎（match_gateway 承载）：网易云/QQ 音乐歌单 → 本地曲库 + 官方歌单。

链路：解析歌单链接/ID → 拉曲目列表（网易走 musicbox 现成管道；QQ 走
enhance_search qq.get_playlist 公开接口）→ 逐首经 musicdl 搜索匹配 →
整轨下载（试听探针 + 音质门槛，与红心下载同标准）→ 落库写标签内嵌封面 →
官方 search 轮询拿 guid → 加入用户指定的官方歌单。

凭据：优先 FNMUSIC_IMPORT_USERNAME/PASSWORD（.env），回落桥接 config.json
（小爱向导第三步录的飞牛账号）。曲库目录复用 detect_library_dir 同款探测
（FNMUSIC_LIBRARY_DIR → music.db shared_library → 拒绝执行）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger("fnmusic_playlist_import")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import match_core  # noqa: E402  复用 .env 加载与三源 URL

_TASK_LOCK = asyncio.Lock()
_TASK: "dict | None" = None          # 单并发：当前导入任务状态
_TASK_ID = ""
_COOLDOWN_S = 120.0                  # 搜索失败后的整任务冷却
_MAX_TRACKS = 200
_MIN_TIER_BY_MODE = {"lossless": 0, "320": 1}
_LOSSLESS_EXTS = ("flac", "ape", "wav", "aiff", "alac")

# 官方 guid 轮询
_LINK_POLL_S = 5.0
_LINK_TIMEOUT_S = 150.0


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


# -------------------------------------------------------------- 输入解析 --

_NETEASE_URL_RE = re.compile(r"music\.163\.com/.*[?&]id=(\d+)|music\.163\.com/playlist/(\d+)")
_QQ_URL_RE = re.compile(r"y\.qq\.com/n/ryqq/playlist/([0-9a-zA-Z]+)|i\.y\.qq\.com/.*[?&]id=(\d+)")


def parse_input(text: str, platform: str = "") -> "tuple[str, str]":
    """歌单链接/纯 ID → (platform, id)。解析不了抛 ValueError。"""
    text = str(text or "").strip()
    platform = str(platform or "").strip().lower()
    if not text:
        raise ValueError("请输入歌单链接或 ID")
    if not platform or platform == "auto":
        if _NETEASE_URL_RE.search(text):
            platform = "netease"
        elif _QQ_URL_RE.search(text):
            platform = "qq"
        elif re.fullmatch(r"\d+", text):
            # 纯数字默认网易（网易云歌单 id 全数字；QQ 走 URL 带入）
            platform = "netease"
        else:
            raise ValueError("无法识别平台：请粘贴网易云/QQ 音乐歌单链接，或选好平台再填纯数字 ID")
    m = _NETEASE_URL_RE.search(text)
    pid = (m.group(1) or m.group(2)) if m else ""
    if platform == "netease":
        if not pid:
            m2 = re.fullmatch(r"(\d+)", text)
            pid = m2.group(1) if m2 else ""
        if not pid:
            raise ValueError("网易云歌单 ID 解析失败")
        return "netease", pid
    m = _QQ_URL_RE.search(text)
    qid = (m.group(1) or m.group(2)) if m else ""
    if platform == "qq":
        if not qid:
            qid = text if re.fullmatch(r"[0-9a-zA-Z]+", text) else ""
        if not qid:
            raise ValueError("QQ 音乐歌单 ID 解析失败")
        return "qq", qid
    raise ValueError(f"不支持的平台 {platform!r}")


# -------------------------------------------------------------- 曲目拉取 --

def _norm(v) -> str:
    return re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】&+/]+", "", str(v or "").lower())


async def fetch_playlist_tracks(platform: str, pid: str) -> dict:
    """拉歌单名与曲目列表（统一形状 {title, artist, album, duration_ms, cover_url}）。"""
    if platform == "qq":
        def _qq():
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from enhance_search.platforms import qq as qq_plat

            data = qq_plat.get_playlist(pid, timeout=20)
            if not data:
                raise RuntimeError("QQ 歌单拉取失败（接口无响应或歌单不可见）")
            tracks = []
            for it in data.get("tracks") or []:
                if not isinstance(it, dict) or not it.get("title"):
                    continue
                tracks.append({
                    "title": str(it.get("title") or ""),
                    "artist": str(it.get("artist") or ""),
                    "album": str(it.get("album") or ""),
                    "duration_ms": int(it.get("duration") or 0),
                    "cover_url": str(it.get("picUrl") or ""),
                })
            if not tracks:
                raise RuntimeError("QQ 歌单里没有可用曲目")
            return {"name": str(data.get("name") or pid), "tracks": tracks}
        return await asyncio.to_thread(_qq)
    if platform == "netease":
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            base = match_core.MUSICBOX_URL
            summary = {}
            try:
                resp = await client.get(
                    f"{base}/api/v1/public-playlists/{pid}",
                    params={"source": "netease", "kind": "playlist"})
                data = resp.json()
                if isinstance(data, dict) and data.get("ok") and isinstance(data.get("data"), dict):
                    summary = data["data"]
            except Exception as exc:  # noqa: BLE001  摘要失败不阻断，曲目才是主体
                logger.info("netease playlist summary failed for %s: %s", pid, exc)
            resp = await client.get(
                f"{base}/api/v1/public-playlists/{pid}/tracks",
                params={"source": "netease", "kind": "playlist"})
            if resp.status_code != 200:
                raise RuntimeError(f"musicbox 曲目接口 HTTP {resp.status_code}（网易组件未启用？）")
            payload = resp.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError(str((payload or {}).get("error") or "musicbox 返回异常"))
            tracks = []
            for row in rows[:_MAX_TRACKS]:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                tracks.append({
                    "title": str(row.get("name") or ""),
                    "artist": str(row.get("artist") or ""),
                    "album": str(row.get("album_name") or ""),
                    "duration_ms": int(row.get("duration_ms") or 0),
                    "cover_url": str(row.get("album_pic_url") or row.get("cover_url") or ""),
                })
            if not tracks:
                raise RuntimeError("网易歌单里没有可用曲目（需要登录态且歌单可见）")
            name = str(summary.get("name") or "") or f"网易云歌单 {pid}"
            return {"name": name, "tracks": tracks}
    raise ValueError(f"不支持的平台 {platform!r}")


# -------------------------------------------------------------- 音质与探测 --

def _quality_tier(ext: str, size: int, duration_s: float) -> int:
    if str(ext or "").lower() in _LOSSLESS_EXTS:
        return 0
    if size > 0 and duration_s > 0:
        return 1 if size * 8 / duration_s >= 256_000 else 2
    return 2


def _min_tier() -> int:
    return _MIN_TIER_BY_MODE.get(_env("FNMUSIC_DOWNLOAD_MIN_QUALITY", "any").lower(), 2)


def _probe_duration_s(path: str) -> "float | None":
    """音频时长（秒）按魔数选 mutagen 解析；失败 None（不阻断）。"""
    try:
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
    except Exception:  # noqa: BLE001  mutagen 缺席不阻断
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head.startswith(b"fLaC"):
            return float(FLAC(path).info.length)
        if head[4:8] == b"ftyp":
            return float(MP4(path).info.length)
        if head.startswith(b"OggS"):
            return float(OggVorbis(path).info.length)
        return float(MP3(path).info.length)
    except Exception:  # noqa: BLE001
        return None


def _sniff_ext(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head.startswith(b"fLaC"):
            return "flac"
        if head[4:8] == b"ftyp":
            return "m4a"
        if head.startswith(b"OggS"):
            return "ogg"
        if head.startswith(b"ID3") or (len(head) > 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            return "mp3"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _library_dir() -> str:
    """曲库目录：env 显式 → music.db shared_library；都拿不到抛错（宁可不导入也不乱写）。"""
    explicit = _env("FNMUSIC_LIBRARY_DIR") or _env("FNMUSIC_TEE_SAVE_DIR")
    if explicit:
        return explicit
    db = _env("FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db")
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
        finally:
            con.close()
        for (path,) in rows:
            if path and os.path.isdir(path):
                return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("import: read shared_library failed: %s", exc)
    raise RuntimeError("无法定位共享曲库目录（设 FNMUSIC_LIBRARY_DIR 后重试）")


def _library_basename(title: str, artist: str) -> str:
    pattern = _env("FNMUSIC_LIBRARY_NAME_PATTERN", "{artist} - {title}")
    stem = pattern.replace("{artist}", artist or "未知艺术家").replace("{title}", title or "未知标题")
    stem = stem.replace("{source}", "").strip()
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", stem).strip(" .") or "untitled"


def _write_tags(path: str, ext: str, title: str, artist: str, album: str, cover: bytes) -> None:
    """写 ID3/FLAC/MP4 标签 + 内嵌封面（尽力而为，失败不回滚音频）。"""
    try:
        if ext == "mp3":
            from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1

            tags = ID3(path)
            tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
            tags.setall("TALB", [TALB(encoding=3, text=album)])
            if cover:
                tags.setall("APIC", [APIC(encoding=3, mime="image/jpeg", type=3,
                                          desc="Cover", data=cover)])
            tags.save()
        elif ext == "flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(path)
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        elif ext in ("m4a", "mp4"):
            from mutagen.mp4 import MP4

            audio = MP4(path)
            audio["\xa9nam"] = [title]
            audio["\xa9ART"] = [artist]
            audio["\xa9alb"] = [album]
            if cover:
                audio["covr"] = [cover]
            audio.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("import: write tags failed for %s: %s", os.path.basename(path), exc)


async def _download_cover(url: str) -> bytes:
    if not url:
        return b""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.debug("import: cover download failed: %s", exc)
    return b""


# -------------------------------------------------------------- 匹配与下载 --

def _pick_candidate(items: list, title: str, artist: str) -> "dict | None":
    """musicdl 搜索结果 → 最优同名候选（标题归一相等 + 歌手重叠 + 音质最好）。"""
    t_norm = _norm(title)
    if not t_norm:
        return None
    a_tokens = [_norm(t) for t in re.split(r"[&/,、\s]+", str(artist or "")) if _norm(t)]
    best, best_key = None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        if not str(it.get("download_url") or "").startswith(("http", "/")):
            continue
        if _norm(it.get("title")) != t_norm:
            continue
        cand_artist = _norm(it.get("artist"))
        if a_tokens and cand_artist and not any(t in cand_artist for t in a_tokens):
            continue
        tier = _quality_tier(str(it.get("ext") or ""), int(it.get("file_size") or 0),
                             float(it.get("duration_s") or 0))
        key = (tier, str(it.get("source") or ""))
        if best_key is None or key < best_key:
            best, best_key = it, key
    return best


async def _search_candidate(client: httpx.AsyncClient, title: str, artist: str) -> "dict | None":
    try:
        resp = await client.get(f"{match_core.MUSICDL_URL}/search",
                                params={"keyword": f"{title} {artist}".strip(), "limit": 10},
                                timeout=40)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("import: search failed for %r: %s", title, exc)
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return _pick_candidate(items, title, artist)


async def _download_stream(client: httpx.AsyncClient, item_id: str, dest: str,
                           nominal_s: float = 0.0) -> "tuple[int, float]":
    """下载整轨到 dest。返回 (字节数, 实测时长秒)。长度不符/试听/过小抛错。"""
    async with client.stream("GET", f"{match_core.MUSICDL_URL}/stream",
                             params={"id": item_id, "proxy": "true"}, timeout=300) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"stream HTTP {resp.status_code}")
        length = resp.headers.get("content-length", "")
        expected = int(length) if length.isdigit() else None
        written = 0
        with open(dest, "wb") as fp:
            async for chunk in resp.aiter_bytes(64 * 1024):
                if chunk:
                    fp.write(chunk)
                    written += len(chunk)
    if written < 1024 or (expected is not None and written != expected):
        raise RuntimeError(f"size mismatch written={written} expected={expected}")
    actual_s = await asyncio.to_thread(_probe_duration_s, dest)
    # 试听片段判定与红心下载同标：实测时长 < 标称 60% 判片段；无标称时用绝对下限
    if actual_s and ((nominal_s and actual_s < nominal_s * 0.6) or actual_s < 30):
        raise RuntimeError(f"preview snippet? duration={actual_s:.1f}s nominal={nominal_s:.0f}s")
    return written, float(actual_s or 0)


# -------------------------------------------------------------- 官方侧 --

def _bridge_config_path() -> str:
    candidates = [
        _env("FNMUSIC_BRIDGE_CONFIG"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "sources-data", "xiaomusic", "bridge", "config.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "sources-data", "data", "xiaomusic", "bridge", "config.json"),
        "/data/xiaomusic/bridge/config.json",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def _official_credentials() -> "tuple[str, str, str]":
    """(base_url, username, password)：.env 优先，回落桥接 config.json。"""
    base = _env("FNMUSIC_BASE_URL") or f"http://{_env('FNMUSIC_NAS_IP', '127.0.0.1')}:5666"
    user = _env("FNMUSIC_IMPORT_USERNAME")
    pwd = _env("FNMUSIC_IMPORT_PASSWORD")
    if user and pwd:
        return base, user, pwd
    path = _bridge_config_path()
    if path:
        try:
            import json

            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            user = user or str(cfg.get("fnmusic_username") or "")
            pwd = pwd or str(cfg.get("fnmusic_password") or "")
            base = _env("FNMUSIC_BASE_URL") or str(cfg.get("fnmusic_base_url") or base)
        except Exception as exc:  # noqa: BLE001
            logger.warning("import: read bridge config failed: %s", exc)
    return base, user, pwd


def _official_client() -> "object | None":
    base, user, pwd = _official_credentials()
    if not user or not pwd:
        return None
    xiaomusic_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "xiaomusic-service")
    if xiaomusic_dir not in sys.path:
        sys.path.insert(0, xiaomusic_dir)
    from fnmusic_client import FnMusicClient

    return FnMusicClient(base_url=base, username=user, password=pwd)


def _norm_title(v) -> str:
    return _norm(v)


async def _official_link_poll(client, title: str, artist: str) -> "str | None":
    """落库后轮询官方搜索拿 guid（官方异步索引）。"""
    deadline = time.monotonic() + _LINK_TIMEOUT_S
    keyword = f"{title} {artist}".strip()
    t_norm = _norm_title(title)
    while time.monotonic() < deadline:
        try:
            rows = await client.search_track(keyword, page_size=20)
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                row_title = str(row.get("title") or row.get("name") or "")
                row_guid = str(row.get("guid") or "")
                if row_guid and _norm_title(row_title) == t_norm:
                    return row_guid
        except Exception as exc:  # noqa: BLE001
            logger.debug("import: official search poll failed: %s", exc)
        await asyncio.sleep(_LINK_POLL_S)
    return ""


# -------------------------------------------------------------- 任务编排 --

def _task_public() -> dict:
    t = _TASK or {}
    return {
        "taskId": _TASK_ID,
        "state": t.get("state", "idle"),
        "name": t.get("name", ""),
        "total": len(t.get("tracks") or []),
        "done": t.get("done", 0),
        "ok": t.get("ok", 0),
        "skipped": t.get("skipped", 0),
        "failed": t.get("failed", []),
        "current": t.get("current", ""),
        "error": t.get("error"),
        "playlistGuid": t.get("playlistGuid", ""),
        "addedGuids": t.get("addedGuids", []),
    }


async def start_import(input_text: str, platform: str, target_guid: str,
                       target_new: str = "", limit: int = 0) -> str:
    global _TASK, _TASK_ID
    async with _TASK_LOCK:
        if _TASK and _TASK.get("state") in ("running",):
            raise RuntimeError("已有导入任务在跑")
        platform, pid = parse_input(input_text, platform)
        playlist = await fetch_playlist_tracks(platform, pid)
        tracks = playlist.get("tracks") or []
        if not tracks:
            raise RuntimeError("歌单里没有可用曲目")
        try:
            limit = int(limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            tracks = tracks[:limit]
        target_guid = str(target_guid or "").strip()
        target_new = str(target_new or "").strip()
        if not target_new and (not target_guid or not re.fullmatch(r"[0-9a-f]{32}", target_guid)):
            raise RuntimeError("请选择目标歌单或填写新建歌单名称")
        if target_new:
            # 自动建目标歌单（官方 POST /playlist/create 实测可用）
            client = _official_client()
            if client is None:
                raise RuntimeError("缺少飞牛凭据（设 FNMUSIC_IMPORT_USERNAME/PASSWORD 或完成小爱向导第三步）")
            await client.login()
            data = await client.api_call("POST", "/playlist/create",
                                         json_body={"name": target_new[:64]})
            target_guid = str((data or {}).get("guid") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", target_guid):
                raise RuntimeError("官方建歌单失败（未返回 guid）")
        _TASK_ID = uuid.uuid4().hex[:12]
        _TASK = {
            "state": "running", "name": playlist.get("name", ""), "platform": platform,
            "pid": pid, "tracks": tracks, "done": 0, "ok": 0, "skipped": 0,
            "failed": [], "current": "", "error": None,
            "playlistGuid": target_guid, "addedGuids": [],
        }
        task = asyncio.get_running_loop().create_task(_run_import())
        task.add_done_callback(_task_finished)
        return _TASK_ID


def _task_finished(task: asyncio.Task) -> None:
    if _TASK is None:
        return
    if _TASK.get("state") == "running":
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            _TASK["state"] = "failed"
            _TASK["error"] = f"{type(exc).__name__}: {exc}"
        else:
            _TASK["state"] = "done"


async def _run_import() -> None:
    t = _TASK
    lib = _library_dir()
    client = _official_client()
    if client is None:
        t["state"] = "failed"
        t["error"] = "缺少飞牛凭据（设 FNMUSIC_IMPORT_USERNAME/PASSWORD 或完成小爱向导第三步）"
        return
    try:
        await client.login()
    except Exception as exc:  # noqa: BLE001
        t["state"] = "failed"
        t["error"] = f"飞牛登录失败: {exc}"
        return
    min_tier = _min_tier()
    scan_path = _env("FNMUSIC_LIBRARY_SCAN_PATH")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        for index, track in enumerate(t["tracks"]):
            if t.get("state") not in ("running",):
                break
            title = str(track.get("title") or "")
            artist = str(track.get("artist") or "")
            t["current"] = f"{index + 1}/{len(t['tracks'])} {title} - {artist}"
            t["done"] = index
            try:
                candidate = await _search_candidate(http, title, artist)
                if candidate is None:
                    t["skipped"] += 1
                    continue
                tier = _quality_tier(str(candidate.get("ext") or ""),
                                     int(candidate.get("file_size") or 0),
                                     float(candidate.get("duration_s") or 0))
                if tier > min_tier:
                    t["failed"].append({"title": title, "artist": artist,
                                        "reason": f"音质不达标（{candidate.get('ext')}）"})
                    continue
                ext0 = str(candidate.get("ext") or "mp3")
                tmp = os.path.join(lib, f".import-{uuid.uuid4().hex}.part")
                nominal_s = float(track.get("duration_ms") or 0) / 1000.0
                try:
                    size, actual_s = await _download_stream(
                        http, str(candidate.get("id")), tmp, nominal_s)
                    sniffed = await asyncio.to_thread(_sniff_ext, tmp)
                    ext = sniffed or ext0
                    cover = await _download_cover(str(candidate.get("cover_url") or "")
                                                  or str(track.get("cover_url") or ""))
                    dest = os.path.join(lib, f"{_library_basename(title, artist)}.{ext}")
                    if os.path.exists(dest):
                        t["skipped"] += 1
                        os.remove(tmp)
                        continue
                    os.replace(tmp, dest)
                    await asyncio.to_thread(_write_tags, dest, ext, title, artist,
                                            str(track.get("album") or ""), cover)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                # 官方收录 → 加进目标歌单
                guid = await _official_link_poll(client, title, artist)
                if not guid:
                    t["failed"].append({"title": title, "artist": artist,
                                        "reason": "官方曲库未收录（稍后可在 App 手动扫描）"})
                    continue
                await client.api_call("POST", "/playlist/add-track",
                                      json_body={"guid": t["playlistGuid"],
                                                 "trackGUIDs": [guid]})
                t["ok"] += 1
                t.setdefault("addedGuids", []).append(guid)
            except Exception as exc:  # noqa: BLE001  单曲失败不拖垮整单
                t["failed"].append({"title": title, "artist": artist,
                                    "reason": str(exc) or type(exc).__name__})
            # 触发官方重扫（配置了扫描接口时）
            if scan_path:
                try:
                    await client.api_call("POST", scan_path.replace("/music/api/v1", ""), json_body={})
                except Exception:  # noqa: BLE001
                    pass
        t["done"] = len(t["tracks"])
        t["current"] = ""
        if t.get("state") == "running":
            t["state"] = "done"


def get_task(task_id: str) -> "dict | None":
    if not _TASK or task_id != _TASK_ID:
        return None
    return _task_public()
