"""曲库歌曲匹配核心（二开功能）：多音源搜索歌词/封面并写回官方数据。

写回目标（与官方同构，不走官方 API——封面/歌词没有公开写入接口）：
- 歌词：music.db 的 `lyric` 表（track_id ↔ stored_guid）+ `meta/lyric-sqlite/`
  分片库（16 片，`lyric-0<stored_guid[0]>.db`，表 lyric_content(guid, content, created_at)，
  content 为 LRC 文本 bytes）；
- 封面：`meta/cover/track/<guid[:2]>/<guid>` 文件 + track.cover_guid 更新。

音源复用现有三服务（按 .env 启用开关）：musicdl（多平台聚合）/ musicbox（网易）/ lx（洛雪）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid

import httpx

logger = logging.getLogger("fnmusic_match")


def _load_dotenv() -> None:
    """加载仓库根 .env（setdefault：进程已有环境变量优先）。

    匹配网关不经 takeover 启动，拿不到其注入的 FNMUSIC_* 配置，必须自读。
    """
    try:
        from env_merge import parse_env_file

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        for k, v in dict(parse_env_file(path)[0]).items():
            if k:
                os.environ.setdefault(k, v)
    except Exception:
        pass


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


META_ROOT = _env("FNMUSIC_META_ROOT", "/var/apps/trim.music/meta")
MUSIC_DB = _env("FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db")
LYRIC_SQ_DIR = os.path.join(META_ROOT, "lyric-sqlite")
COVER_ROOT = os.path.join(META_ROOT, "cover")

MUSICDL_URL = _env("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768")
MUSICBOX_URL = _env("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770")
LX_URL = _env("FNMUSIC_LX_URL", "http://127.0.0.1:8772")
ONLINE_SOURCES = _env("FNMUSIC_ONLINE_SOURCES", "")
LX_SOURCES = _env("LX_SOURCES", "")
SEARCH_TIMEOUT = float(_env("FNMUSIC_SEARCH_TIMEOUT", "15") or 15)


def _enabled_sources() -> list[str]:
    # qq/kugou 默认开启（QRC/KRC 逐字歌词与封面质量更好），排在前作为
    # 标题吻合时的优先候选；musicdl/netease/lx 维持原有默认关闭。
    out = []
    if _env("FNMUSIC_QQ_ENABLED", "true").lower() in ("true", "1", "yes"):
        out.append("qq")
    if _env("FNMUSIC_KUGOU_ENABLED", "true").lower() in ("true", "1", "yes"):
        out.append("kugou")
    if _env("FNMUSIC_MUSICDL_ENABLED", "false").lower() in ("true", "1", "yes"):
        out.append("musicdl")
    if _env("FNMUSIC_NETEASE_ENABLED", "false").lower() in ("true", "1", "yes"):
        out.append("netease")
    if _env("FNMUSIC_LX_ENABLED", "false").lower() in ("true", "1", "yes"):
        out.append("lx")
    return out


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(MUSIC_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _lyric_db_path(stored_guid: str) -> str:
    # 16 分片：lyric-00.db ~ lyric-0f.db，分片号 = stored_guid 第一个 hex 字符
    return os.path.join(LYRIC_SQ_DIR, f"lyric-0{stored_guid[0]}.db")


# ---------------------------------------------------------------------------
# 曲库列表
# ---------------------------------------------------------------------------

_LIST_SQL = """
SELECT t.guid AS guid, t.title AS title, t.cover_guid AS cover_guid,
       t.duration_ms AS duration_ms, af.path AS path,
       COALESCE(al.name, '') AS album,
       COALESCE(GROUP_CONCAT(ar.name, ' / '), '') AS artist,
       CASE WHEN EXISTS (SELECT 1 FROM lyric l WHERE l.track_id = t.id) THEN 1 ELSE 0 END AS has_lyric
FROM track t
JOIN audio_file af ON af.id = t.audio_file_id
LEFT JOIN album al ON al.id = t.album_id
LEFT JOIN track_artist ta ON ta.track_id = t.id
LEFT JOIN artist ar ON ar.id = ta.artist_id
WHERE t.is_admin_deleted = 0 AND t.is_audio_file_deleted = 0
"""


def list_tracks(page: int = 1, size: int = 50, keyword: str = "", filter_: str = "all") -> dict:
    page = max(1, page)
    size = min(max(1, size), 200)
    where = ""
    kw = (keyword or "").strip()
    if kw:
        esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (t.title LIKE ? ESCAPE '\\' OR af.path LIKE ? ESCAPE '\\')"
    having = ""
    if filter_ == "missing_lyric":
        having = " HAVING has_lyric = 0"
    elif filter_ == "missing_cover":
        having = " HAVING has_cover = 0"
    elif filter_ == "missing_any":
        having = " HAVING has_lyric = 0 OR has_cover = 0"

    base = _LIST_SQL.replace(
        "CASE WHEN EXISTS (SELECT 1 FROM lyric l WHERE l.track_id = t.id) THEN 1 ELSE 0 END AS has_lyric",
        "CASE WHEN EXISTS (SELECT 1 FROM lyric l WHERE l.track_id = t.id) THEN 1 ELSE 0 END AS has_lyric, "
        "CASE WHEN t.cover_guid IS NOT NULL AND t.cover_guid != '' THEN 1 ELSE 0 END AS has_cover",
    )
    params: list = [f"%{esc}%", f"%{esc}%"] if kw else []
    count_filter = {
        "missing_lyric": " AND sub.has_lyric = 0",
        "missing_cover": " AND sub.has_cover = 0",
        "missing_any": " AND (sub.has_lyric = 0 OR sub.has_cover = 0)",
    }.get(filter_, "")
    conn = _db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM ({base}{where} GROUP BY t.id) sub "
            f"WHERE 1=1{count_filter})",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"{base}{where} GROUP BY t.id{having} ORDER BY t.id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()
        items = [
            {
                "guid": r["guid"],
                "title": r["title"] or "",
                "artist": r["artist"] or "",
                "album": r["album"] or "",
                "durationMs": int(r["duration_ms"] or 0),
                "path": r["path"] or "",
                "hasLyric": bool(r["has_lyric"]),
                "hasCover": bool(r["has_cover"]),
            }
            for r in rows
        ]
        return {"total": total, "page": page, "size": size, "items": items}
    finally:
        conn.close()


def get_track(guid: str) -> dict | None:
    conn = _db()
    try:
        r = conn.execute(
            f"{_LIST_SQL} AND t.guid = ? GROUP BY t.id", (guid,)
        ).fetchone()
        if not r:
            return None
        artist = conn.execute(
            "SELECT COALESCE(GROUP_CONCAT(ar.name, ' / '), '') AS a FROM track_artist ta "
            "JOIN artist ar ON ar.id = ta.artist_id WHERE ta.track_id = "
            "(SELECT id FROM track WHERE guid = ?)",
            (guid,),
        ).fetchone()
        return {
            "guid": r["guid"], "title": r["title"] or "",
            "artist": artist["a"] if artist else "", "album": r["album"] or "",
            "durationMs": int(r["duration_ms"] or 0), "path": r["path"] or "",
            "hasLyric": bool(r["has_lyric"]),
            "hasCover": bool(r["cover_guid"]),
        }
    finally:
        conn.close()


def _track_row(guid: str) -> sqlite3.Row | None:
    conn = _db()
    try:
        return conn.execute("SELECT * FROM track WHERE guid = ? LIMIT 1", (guid,)).fetchone()
    finally:
        conn.close()


def _track_artist_names(track_id: int) -> str:
    conn = _db()
    try:
        r = conn.execute(
            "SELECT COALESCE(GROUP_CONCAT(ar.name, ' / '), '') AS a FROM track_artist ta "
            "JOIN artist ar ON ar.id = ta.artist_id WHERE ta.track_id = ?",
            (track_id,),
        ).fetchone()
        return r["a"] if r else ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 写回：歌词（music.db lyric 表 + lyric-sqlite 分片库）
# ---------------------------------------------------------------------------

def write_lyric(track_id: int, text: str) -> None:
    """写入歌词：新 stored_guid → lyric 表 → 分片库写入 → 删旧。"""
    text = (text or "").replace("\x00", "").strip()
    if not text:
        raise ValueError("歌词内容为空")
    new_guid = uuid.uuid4().hex
    conn = _db()
    try:
        row = conn.execute(
            "SELECT stored_guid FROM lyric WHERE track_id = ? LIMIT 1", (track_id,)
        ).fetchone()
        old = str(row[0]) if row and row[0] else None
        if old and not re.fullmatch(r"[0-9a-f]{32}", old):
            old = None
        if row:
            conn.execute(
                "UPDATE lyric SET stored_guid = ?, source = 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE track_id = ?",
                (new_guid, track_id),
            )
        else:
            conn.execute(
                "INSERT INTO lyric (id, guid, track_id, source, stored_guid, created_at, updated_at) "
                "VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM lyric), ?, ?, 1, ?, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (uuid.uuid4().hex, track_id, new_guid),
            )
        conn.commit()
    finally:
        conn.close()

    # 分片库写入（REPLACE 幂等；分片文件/表缺失时自建——官方预建 16 库，防御新环境）
    path = _lyric_db_path(new_guid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lconn = sqlite3.connect(path, timeout=10)
    try:
        lconn.execute("PRAGMA busy_timeout=5000")
        lconn.execute(
            "CREATE TABLE IF NOT EXISTS lyric_content "
            "(guid TEXT PRIMARY KEY, content BLOB, created_at DATETIME)"
        )
        lconn.execute(
            "REPLACE INTO lyric_content (guid, content, created_at) VALUES (?, ?, datetime('now'))",
            (new_guid, text.encode("utf-8")),
        )
        lconn.commit()
    finally:
        lconn.close()

    # 删旧分片行（不同 guid 时）
    if old and old != new_guid:
        try:
            opath = _lyric_db_path(old)
            if os.path.exists(opath):
                oconn = sqlite3.connect(opath, timeout=10)
                try:
                    oconn.execute("PRAGMA busy_timeout=5000")
                    oconn.execute("DELETE FROM lyric_content WHERE guid = ?", (old,))
                    oconn.commit()
                finally:
                    oconn.close()
        except Exception as e:
            logger.warning("Delete old lyric content failed for %s: %s", old, e)


def read_lyric(track_id: int) -> str:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT stored_guid FROM lyric WHERE track_id = ? LIMIT 1", (track_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return ""
    sg = str(row[0])
    path = _lyric_db_path(sg)
    if not os.path.exists(path):
        return ""
    lconn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        r = lconn.execute(
            "SELECT content FROM lyric_content WHERE guid = ?", (sg,)
        ).fetchone()
    finally:
        lconn.close()
    if not r or r[0] is None:
        return ""
    raw = r[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    # JSON 结构化歌词转纯文本行（保留时间轴）
    s = str(raw).strip()
    if s.startswith("{"):
        try:
            import json

            data = json.loads(s)
            parts = []
            for seg in data.get("c") or []:
                if isinstance(seg, dict) and seg.get("tx") is not None:
                    parts.append(str(seg.get("tx") or ""))
            if parts:
                return "".join(parts)
        except Exception:
            pass
    return s


# ---------------------------------------------------------------------------
# 写回：封面（文件 + track.cover_guid）
# ---------------------------------------------------------------------------

def write_cover(track_id: int, image: bytes) -> None:
    if not image:
        raise ValueError("封面内容为空")
    new_guid = uuid.uuid4().hex
    dest_dir = os.path.join(COVER_ROOT, "track", new_guid[:2])
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, new_guid)
    fd, tmp = tempfile.mkstemp(prefix=".cover-", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    conn = _db()
    try:
        row = conn.execute(
            "SELECT cover_guid FROM track WHERE id = ?", (track_id,)
        ).fetchone()
        old = str(row[0]) if row and row[0] else None
        conn.execute(
            "UPDATE track SET cover_guid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_guid, track_id),
        )
        conn.commit()
    finally:
        conn.close()

    if old and old != new_guid:
        try:
            old_path = os.path.join(COVER_ROOT, "track", old[:2], old)
            if os.path.exists(old_path):
                os.unlink(old_path)
        except OSError as e:
            logger.warning("Delete old cover failed for %s: %s", old, e)


# ---------------------------------------------------------------------------
# 音源搜索 + 候选取数
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】<>《》{}]", "", s)
    return s


def _titles_match(local: str, cand: str) -> bool:
    a, b = _norm(local), _norm(cand)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 2 and shorter in longer


def _build_keyword(title: str, artist: str) -> str:
    return " ".join(x for x in (title, artist) if x).strip()


async def _search_musicdl(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    params: dict = {"keyword": keyword, "limit": 10}
    if ONLINE_SOURCES:
        params["sources"] = ONLINE_SOURCES
    r = await client.get(f"{MUSICDL_URL}/search", params=params, timeout=SEARCH_TIMEOUT)
    if r.status_code != 200:
        return []
    data = r.json()
    out = []
    for it in (data.get("items") or []) if isinstance(data, dict) else []:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        if not sid:
            continue
        out.append({
            "source": "musicdl", "id": sid,
            "title": str(it.get("title") or ""),
            "artist": str(it.get("artist") or ""),
        })
    return out


async def _search_netease(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    r = await client.get(
        f"{MUSICBOX_URL}/api/v1/search",
        params={"keyword": keyword, "limit": 10, "type": "song"},
        timeout=SEARCH_TIMEOUT,
    )
    if r.status_code != 200:
        return []
    data = r.json()
    if not isinstance(data, dict) or data.get("ok") is False:
        return []
    out = []
    for it in (data.get("data") or []):
        if not isinstance(it, dict):
            continue
        sid = str(it.get("song_id") or it.get("id") or "")
        if not sid:
            continue
        artist = it.get("artist")
        if isinstance(artist, list):
            artist = " / ".join(str(x) for x in artist if x)
        out.append({
            "source": "netease", "id": sid,
            "title": str(it.get("song_name") or it.get("title") or ""),
            "artist": str(artist or ""),
        })
    return out


async def _search_lx(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    params: dict = {"keyword": keyword, "limit": 10}
    if LX_SOURCES:
        params["sources"] = LX_SOURCES
    r = await client.get(f"{LX_URL}/api/v1/search", params=params, timeout=SEARCH_TIMEOUT)
    if r.status_code != 200:
        return []
    data = r.json()
    if not isinstance(data, dict) or data.get("ok") is False:
        return []
    out = []
    for it in (data.get("items") or []):
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        if not sid:
            continue
        artist = it.get("artist")
        if isinstance(artist, list):
            artist = " / ".join(str(x) for x in artist if x)
        out.append({
            "source": "lx", "id": sid,
            "title": str(it.get("title") or ""),
            "artist": str(artist or ""),
        })
    return out


def _enhance_platform(src: str):
    """按需加载 enhance_search 平台模块（纯标准库，取自 FnMusicEnhance）。"""
    try:
        import enhance_search.platforms as reg
        return reg.SOURCE_REGISTRY.get(src, {}).get("impl")
    except Exception as e:  # 包缺失/导入失败时该源静默不可用
        logger.warning("enhance_search %s unavailable: %s", src, type(e).__name__)
        return None


def _map_enhance_song(it: dict, src: str) -> dict | None:
    sid = str(it.get("id") or "")
    if not sid:
        return None
    return {
        "source": src, "id": sid,
        "title": str(it.get("title") or ""),
        "artist": str(it.get("artist") or ""),
        "album": str(it.get("album") or ""),
        "duration": int(it.get("duration") or 0),
        "cover_url": str(it.get("picUrl") or ""),
        "internal": it.get("internal") if isinstance(it.get("internal"), dict) else {},
    }


async def _search_enhance(client: httpx.AsyncClient, keyword: str, src: str) -> list[dict]:
    impl = _enhance_platform(src)
    if impl is None:
        return []
    items = await asyncio.to_thread(impl.search_songs, keyword, 1, 10, SEARCH_TIMEOUT)
    out = []
    for it in items or []:
        if isinstance(it, dict):
            cand = _map_enhance_song(it, src)
            if cand:
                out.append(cand)
    return out


async def _search_qq(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    return await _search_enhance(client, keyword, "qq")


async def _search_kugou(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    return await _search_enhance(client, keyword, "kugou")


_SEARCHERS = {
    "qq": _search_qq, "kugou": _search_kugou,
    "musicdl": _search_musicdl, "netease": _search_netease, "lx": _search_lx,
}


def _structured_to_lrc(lines) -> str:
    """enhance_search structured 行数组 → 行级 LRC 文本（词级 payload 拼整行）。"""
    if not lines:
        return ""
    out = []
    for item in lines:
        if not isinstance(item, list) or len(item) < 3:
            continue
        start = item[0]
        payload = item[2]
        if isinstance(payload, list):
            text = "".join(
                w[2] for w in payload if isinstance(w, list) and len(w) > 2
            )
        else:
            text = str(payload)
        if not text.strip():
            continue
        out.append(
            "[%02d:%02d.%02d]%s"
            % (int(start) // 60000, (int(start) % 60000) // 1000,
               (int(start) % 1000) // 10, text)
        )
    return "\n".join(out)


async def _enhance_lyric(src: str, cand: dict) -> str:
    """取 enhance_search 源歌词（QRC/KRC 逐字优先），转为行级 LRC 文本。"""
    impl = _enhance_platform(src)
    if impl is None:
        return ""
    song = {
        "songId": cand.get("id"), "id": cand.get("id"),
        "hash": (cand.get("internal") or {}).get("hash", ""),
        "title": cand.get("title") or "", "album": cand.get("album") or "",
        "artist": cand.get("artist") or "", "duration": int(cand.get("duration") or 0),
        "internal": cand.get("internal") or {},
    }
    try:
        d = await asyncio.to_thread(impl.get_lyrics, song, SEARCH_TIMEOUT)
    except Exception as e:
        logger.warning("Match %s get_lyrics failed: %s", src, type(e).__name__)
        return ""
    return _structured_to_lrc((d or {}).get("original"))


async def search_candidates(client: httpx.AsyncClient, keyword: str) -> list[dict]:
    out: list[dict] = []
    for src in _enabled_sources():
        fn = _SEARCHERS.get(src)
        if not fn:
            continue
        try:
            out.extend(await fn(client, keyword))
        except Exception as e:
            logger.warning("Match search failed on %s: %s", src, type(e).__name__)
    return out


async def fetch_candidate_detail(client: httpx.AsyncClient, cand: dict) -> dict:
    """取候选的歌词与封面 URL（按音源不同）。返回 {lyric, cover_url}。"""
    src, sid = cand.get("source"), cand.get("id")
    lyric, cover_url = "", ""
    try:
        if src == "musicdl":
            r = await client.get(f"{MUSICDL_URL}/info", params={"id": sid}, timeout=10.0)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ok") is not False:
                    lyric = str(d.get("lyric") or "")
                    cover_url = str(d.get("cover_url") or "")
                    if not cand.get("artist"):
                        cand["artist"] = str(d.get("artist") or "")
        elif src == "netease":
            r = await client.get(f"{MUSICBOX_URL}/api/v1/song/{sid}/lyric", timeout=10.0)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ok") is not False:
                    lyric = str((d.get("data") or {}).get("lyric") or "")
            r = await client.get(f"{MUSICBOX_URL}/api/v1/song/{sid}/info", timeout=10.0)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ok") is not False:
                    data = d.get("data") if isinstance(d.get("data"), dict) else {}
                    al = data.get("al") if isinstance(data.get("al"), dict) else {}
                    cover_url = str(al.get("picUrl") or "")
        elif src == "lx":
            r = await client.get(f"{LX_URL}/api/v1/track/info", params={"id": sid}, timeout=10.0)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ok") is not False:
                    inner = d.get("data") if isinstance(d.get("data"), dict) else {}
                    lyric = str(inner.get("lyric") or "")
                    cover_url = str(inner.get("cover_url") or "")
                    if not lyric:
                        lr = await client.get(f"{LX_URL}/api/v1/track/lyric", params={"id": sid}, timeout=10.0)
                        if lr.status_code == 200:
                            ld = lr.json()
                            if isinstance(ld, dict) and ld.get("ok") is not False:
                                lyric = str((ld.get("data") or {}).get("lyric") or "")
        elif src == "qq":
            # 搜索结果已带 y.gtimg.cn 专辑封面；歌词走 GetPlayLyricInfo QRC 逐字
            lyric = await _enhance_lyric("qq", cand)
            cover_url = str(cand.get("cover_url") or "")
        elif src == "kugou":
            # 歌词走 KRC 签名接口（需 internal.hash）；封面来自搜索结果
            lyric = await _enhance_lyric("kugou", cand)
            cover_url = str(cand.get("cover_url") or "")
    except Exception as e:
        logger.warning("Match candidate detail failed %s/%s: %s", src, sid, type(e).__name__)
    return {"lyric": lyric.strip(), "cover_url": cover_url.strip()}


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    if not url:
        return None
    try:
        r = await client.get(url, timeout=15.0, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/*, */*",
        })
        if r.status_code == 200 and r.content and len(r.content) > 500:
            ct = r.headers.get("content-type", "")
            if "image" in ct or ct == "" or len(r.content) > 5000:
                return r.content
    except Exception as e:
        logger.warning("Cover download failed: %s", type(e).__name__)
    return None


# ---------------------------------------------------------------------------
# 单曲匹配
# ---------------------------------------------------------------------------

async def match_one(client: httpx.AsyncClient, guid: str, wants: list[str]) -> dict:
    """匹配一首歌：搜候选 → 标题校验 → 写歌词/封面。返回结果 dict。"""
    result = {"guid": guid, "ok": False, "lyricOk": False, "coverOk": False,
              "error": "", "matchedTitle": "", "matchedArtist": "", "keyword": "",
              "source": ""}
    track = _track_row(guid)
    if not track:
        result["error"] = "未找到曲目"
        return result
    title = str(track["title"] or "")
    artist = _track_artist_names(int(track["id"]))
    keyword = _build_keyword(title, artist)
    result["keyword"] = keyword
    if not keyword:
        result["error"] = "无匹配关键词"
        return result

    candidates = await search_candidates(client, keyword)
    if not candidates:
        result["error"] = "音源无搜索结果"
        return result

    # 标题相似度筛选（保守：本地标题必须与候选标题互含/相等）
    picked = None
    for cand in candidates:
        if _titles_match(title, cand.get("title") or ""):
            picked = cand
            break
    if picked is None:
        result["error"] = f"无标题吻合的候选（{len(candidates)} 个）"
        return result
    result["matchedTitle"] = picked.get("title") or ""
    result["matchedArtist"] = picked.get("artist") or ""
    result["source"] = str(picked.get("source") or "")

    detail = await fetch_candidate_detail(client, picked)
    ok_any = False

    if "lyric" in wants and detail.get("lyric"):
        try:
            await asyncio.to_thread(write_lyric, int(track["id"]), detail["lyric"])
            result["lyricOk"] = True
            ok_any = True
        except Exception as e:
            result["error"] = f"歌词写入失败: {type(e).__name__}"

    if "cover" in wants and detail.get("cover_url"):
        image = await _download_image(client, detail["cover_url"])
        if image:
            try:
                await asyncio.to_thread(write_cover, int(track["id"]), image)
                result["coverOk"] = True
                ok_any = True
            except Exception as e:
                result["error"] = f"封面写入失败: {type(e).__name__}"
        elif "cover" in wants and not result["error"]:
            result["error"] = "封面下载失败"

    result["ok"] = ok_any
    if ok_any:
        result["error"] = ""
    return result


# ---------------------------------------------------------------------------
# 去重：按歌名归组，保留音质最好，其余移回收站并迁移引用
# ---------------------------------------------------------------------------

TRASH_DIR = os.path.join(
    _env("FNMUSIC_HOME") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    "trash_duplicate",
)

# 格式音质分档：无损 > 有损（同档内再比码率）
_LOSSLESS = {"flac", "wav", "ape", "wv", "aiff", "dff", "dsf", "tta", "alac"}
_LOSSY = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}


def _norm_title(s: str) -> str:
    s = (s or "").lower()
    return re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】]+", "", s)


def audio_quality(path: str) -> dict:
    """读取音频音质：{format, lossless, bitrate, size}。

    bitrate：无损用 采样率×位深，有损用 mutagen 码率；读不到且有时长时用
    size/duration 估算，全失败给 0（排序退化为文件大小）。
    """
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    fmt = ext or "unknown"
    lossless = fmt in _LOSSLESS
    bitrate = 0
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        info = getattr(audio, "info", None)
        if info is not None:
            if lossless:
                sr = int(getattr(info, "sample_rate", 0) or 0)
                bits = int(getattr(info, "bits_per_sample", 0) or 0)
                bitrate = sr * bits if sr and bits else int(getattr(info, "bitrate", 0) or 0)
            else:
                bitrate = int(getattr(info, "bitrate", 0) or 0)
    except Exception:
        pass
    return {"format": fmt, "lossless": lossless, "bitrate": bitrate,
            "size": os.path.getsize(path) if os.path.exists(path) else 0}


def _quality_sort_key(item: dict) -> tuple:
    """排序 key（越大越优）：无损 > 码率 > 大小。"""
    return (
        1 if item.get("lossless") else 0,
        int(item.get("bitrate") or 0),
        int(item.get("size") or 0),
    )


def scan_duplicates() -> dict:
    """按歌名归组找重复：title 归一化相同即同组，组内按音质排序。

    返回 {groups: [{normKey, title, keep, remove[]}], totalGroups, totalExtra}。
    """
    conn = _db()
    try:
        rows = conn.execute(
            '''
            SELECT t.id, t.guid, t.title, t.audio_file_id,
                   af.path, af.size, af.duration_ms,
                   (SELECT COUNT(*) FROM favorite_track f WHERE f.track_id = t.id) AS fav_count,
                   (SELECT COUNT(*) FROM playlist_track pt WHERE pt.track_id = t.id) AS pl_count
            FROM track t
            JOIN audio_file af ON af.id = t.audio_file_id
            WHERE t.is_admin_deleted = 0 AND t.is_audio_file_deleted = 0
              AND t.title IS NOT NULL AND TRIM(t.title) != ''
            '''
        ).fetchall()
    finally:
        conn.close()

    groups: dict = {}
    for r in rows:
        key = _norm_title(str(r["title"] or ""))
        if not key:
            continue
        groups.setdefault(key, []).append({
            "trackId": r["id"], "guid": r["guid"], "title": r["title"],
            "path": r["path"] or "", "size": int(r["size"] or 0),
            "durationMs": int(r["duration_ms"] or 0),
            "favCount": int(r["fav_count"] or 0), "plCount": int(r["pl_count"] or 0),
        })

    out = []
    total_extra = 0
    for key, items in groups.items():
        if len(items) < 2:
            continue
        for it in items:
            q = audio_quality(it["path"])
            it.update(q)
            if not it["bitrate"] and it["durationMs"]:
                it["bitrate"] = int(it["size"] * 8 * 1000 / it["durationMs"])
        items.sort(key=_quality_sort_key, reverse=True)
        # 音质完全并列时按路径字典序稳定化，保证确定性
        top = items[0]
        tied = [x for x in items[1:] if _quality_sort_key(x) == _quality_sort_key(top)]
        rest = [x for x in items[1:] if _quality_sort_key(x) != _quality_sort_key(top)]
        tied.sort(key=lambda x: x["path"])
        ordered = [top] + tied + rest
        out.append({"normKey": key, "title": ordered[0]["title"], "keep": ordered[0],
                    "remove": ordered[1:]})
        total_extra += len(ordered) - 1
    out.sort(key=lambda g: -len(g["remove"]))
    return {"groups": out, "totalGroups": len(out), "totalExtra": total_extra}


def _merge_play_history(conn: sqlite3.Connection, old_id: int, keep_id: int) -> None:
    """播放历史合并：①只有 old 有历史的用户迁移行；②双方都有的用户 keep 吸收
    old 计数并删除 old 行——两步顺序保证每个用户只留一行且计数不丢。"""
    conn.execute(
        "UPDATE play_history SET track_id = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE track_id = ? AND user_id NOT IN "
        "(SELECT user_id FROM play_history WHERE track_id = ?)",
        (keep_id, old_id, keep_id),
    )
    conn.execute(
        '''
        UPDATE play_history
        SET play_count = play_count + (
            SELECT COALESCE(ph2.play_count, 0) FROM play_history ph2
            WHERE ph2.track_id = ? AND ph2.user_id = play_history.user_id),
            updated_at = CURRENT_TIMESTAMP
        WHERE track_id = ?
          AND user_id IN (SELECT user_id FROM play_history WHERE track_id = ?)
        ''',
        (old_id, keep_id, old_id),
    )
    conn.execute("DELETE FROM play_history WHERE track_id = ?", (old_id,))


def _migrate_refs(conn: sqlite3.Connection, old_id: int, keep_id: int) -> None:
    """把 old track 的收藏/歌单/播放历史引用迁到 keep（先删冲突避免唯一约束）。"""
    conn.execute(
        "DELETE FROM favorite_track WHERE track_id = ? AND user_id IN "
        "(SELECT user_id FROM favorite_track WHERE track_id = ?)",
        (old_id, keep_id),
    )
    conn.execute(
        "UPDATE favorite_track SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE track_id = ?",
        (keep_id, old_id),
    )
    conn.execute(
        "DELETE FROM playlist_track WHERE track_id = ? AND playlist_id IN "
        "(SELECT playlist_id FROM playlist_track WHERE track_id = ?)",
        (old_id, keep_id),
    )
    conn.execute(
        "UPDATE playlist_track SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE track_id = ?",
        (keep_id, old_id),
    )
    # 歌词偏好/偏移：old 歌词将被移除，指向 old 的偏好直接删（不迁移到 keep 的歌词）
    conn.execute("DELETE FROM user_track_lyric_preference WHERE track_id = ?", (old_id,))
    conn.execute("DELETE FROM user_track_lyric_offset WHERE track_id = ?", (old_id,))
    _merge_play_history(conn, old_id, keep_id)


def _remove_lyric_of(conn: sqlite3.Connection, track_id: int) -> None:
    """删除该 track 的歌词行 + 分片内容。"""
    rows = conn.execute("SELECT stored_guid FROM lyric WHERE track_id = ?", (track_id,)).fetchall()
    if not rows:
        return
    conn.execute("DELETE FROM lyric WHERE track_id = ?", (track_id,))
    for (sg,) in rows:
        if not sg or not re.fullmatch(r"[0-9a-f]{32}", str(sg)):
            continue
        try:
            path = _lyric_db_path(str(sg))
            if os.path.exists(path):
                lconn = sqlite3.connect(path, timeout=10)
                try:
                    lconn.execute("PRAGMA busy_timeout=5000")
                    lconn.execute("DELETE FROM lyric_content WHERE guid = ?", (str(sg),))
                    lconn.commit()
                finally:
                    lconn.close()
        except Exception as e:
            logger.warning("Delete lyric content failed %s: %s", sg, e)


def _transfer_lyric(conn: sqlite3.Connection, old_id: int, keep_id: int) -> None:
    """歌词：keep 没有则把 old 的行转挂 keep（分片不动）；都有则删 old 的。"""
    keep_has = conn.execute("SELECT 1 FROM lyric WHERE track_id = ? LIMIT 1", (keep_id,)).fetchone()
    if keep_has:
        _remove_lyric_of(conn, old_id)
    else:
        conn.execute(
            "UPDATE lyric SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE track_id = ?",
            (keep_id, old_id),
        )


def remove_duplicate(remove_track_id: int, keep_track_id: int) -> dict:
    """执行去重：校验歌名一致 → 引用迁移 → 歌词处置 → 删库记录 → 文件移回收站。

    返回 {removedTrackId, keptTrackId, moved, trashDir}。
    """
    if remove_track_id == keep_track_id:
        raise ValueError("remove 与 keep 相同")
    conn = _db()
    try:
        rm = conn.execute(
            "SELECT t.id, t.title, t.audio_file_id, af.path FROM track t "
            "JOIN audio_file af ON af.id = t.audio_file_id WHERE t.id = ?",
            (remove_track_id,),
        ).fetchone()
        kp = conn.execute("SELECT title FROM track WHERE id = ?", (keep_track_id,)).fetchone()
        if not rm or not kp:
            raise ValueError("track 不存在")
        if _norm_title(str(rm["title"])) != _norm_title(str(kp[0])):
            raise ValueError("歌名不一致，拒绝当作同一首歌处理")
        src_path = rm["path"]
        audio_file_id = rm["audio_file_id"]
    finally:
        conn.close()

    # 先移文件（曲库与回收站可能跨挂载点：os.replace 失败用 shutil.move 兜底），
    # 失败即中止且不动库；之后库事务失败则把文件移回原位，保证两步一致。
    moved = []
    back = []  # (dest, src) 用于库事务失败回滚
    if src_path and os.path.exists(src_path):
        os.makedirs(TRASH_DIR, exist_ok=True)
        base = os.path.basename(src_path)
        dest = os.path.join(TRASH_DIR, base)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(TRASH_DIR, str(n) + "_" + base)
            n += 1
        try:
            os.replace(src_path, dest)
        except OSError:
            shutil.move(src_path, dest)
        moved.append(dest)
        back.append((dest, src_path))
        sidecar = os.path.splitext(src_path)[0] + ".lrc"
        if os.path.exists(sidecar):
            sdest = os.path.splitext(dest)[0] + ".lrc"
            try:
                try:
                    os.replace(sidecar, sdest)
                except OSError:
                    shutil.move(sidecar, sdest)
                moved.append(sdest)
                back.append((sdest, sidecar))
            except OSError:
                pass

    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_refs(conn, remove_track_id, keep_track_id)
            _transfer_lyric(conn, remove_track_id, keep_track_id)
            conn.execute("DELETE FROM track_artist WHERE track_id = ?", (remove_track_id,))
            conn.execute("DELETE FROM track_genre WHERE track_id = ?", (remove_track_id,))
            conn.execute("DELETE FROM track WHERE id = ?", (remove_track_id,))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM track WHERE audio_file_id = ?", (audio_file_id,)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute("DELETE FROM audio_file WHERE id = ?", (audio_file_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            for d, s_path in back:
                try:
                    shutil.move(d, s_path)
                except OSError:
                    pass
            raise
    finally:
        conn.close()
    return {"removedTrackId": remove_track_id, "keptTrackId": keep_track_id,
            "moved": moved, "trashDir": TRASH_DIR}


# ---------------------------------------------------------------------------
# 批量任务（进程内）
# ---------------------------------------------------------------------------

_TASKS: dict[str, dict] = {}
_TASK_TTL = 3600.0


def _prune_tasks() -> None:
    now = time.monotonic()
    for k, t in list(_TASKS.items()):
        if now - t["createdAt"] > _TASK_TTL and not t.get("running"):
            _TASKS.pop(k, None)


def start_task(guids: list[str], wants: list[str]) -> str:
    _prune_tasks()
    task_id = uuid.uuid4().hex[:12]
    _TASKS[task_id] = {
        "id": task_id, "guids": list(guids), "wants": list(wants),
        "results": [], "running": True, "createdAt": time.monotonic(),
        "current": "",
    }
    asyncio.get_running_loop().create_task(_run_task(task_id))
    return task_id


async def _run_task(task_id: str) -> None:
    t = _TASKS.get(task_id)
    if not t:
        return
    async with httpx.AsyncClient() as client:
        for guid in t["guids"]:
            t["current"] = guid
            try:
                res = await match_one(client, guid, t["wants"])
            except Exception as e:
                res = {"guid": guid, "ok": False, "lyricOk": False, "coverOk": False,
                       "error": f"异常: {type(e).__name__}", "matchedTitle": "", "matchedArtist": ""}
            t["results"].append(res)
    t["running"] = False
    t["current"] = ""
    logger.info("Match task %s finished: %d/%d ok", task_id,
                sum(1 for r in t["results"] if r.get("ok")), len(t["guids"]))


def get_task(task_id: str) -> dict | None:
    t = _TASKS.get(task_id)
    if not t:
        return None
    return {
        "id": t["id"], "total": len(t["guids"]), "done": len(t["results"]),
        "running": t["running"], "current": t["current"], "wants": t["wants"],
        "results": t["results"],
        "okCount": sum(1 for r in t["results"] if r.get("ok")),
    }
