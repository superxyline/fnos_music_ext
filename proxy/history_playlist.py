"""最近常听：播放历史驱动的只读虚拟歌单（FNMUSIC_HISTORY_PLAYLIST_*）。

职责：把官方 music.db 的 play_history（含 play_count 播放次数）与代理侧在线
播放历史（play_history/<user>.json）合并打分，生成一张「最近常听」虚拟歌单
注入音乐页——数据全在本地，不依赖网易登录态与 LLM。

契约与 publicplaylists/recommend 同形（见 HANDOFF §2）：
- guid 自包含：online:playlist:history:recent（单张，按请求用户取数）；
- 卡片 coverId 用确定性 md5 伪装 track_+32hex（封面端点经 resolve_real_guid
  反解到第一首可用曲目）；
- 曲目缓存落 history_cache/<user>.json（带 tracks 键），ensure_registry_warm
  与 _lookup_playlist_cache_track 直接复用现有扫描管道；
- 只读：音乐页对它加歌/移歌被 proxy 吸收。

打分：官方播放次数为主（play_count），在线播放每曲计 1 次；同曲（标题+歌手
归一化相等）合并计数，官方形态优先。排序 次数 desc → 最近播放 desc。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from uuid import uuid4

logger = logging.getLogger("proxy.history_playlist")

HISTORY_GUID_PREFIX = "online:playlist:history:"
PLAYLIST_GUID = f"{HISTORY_GUID_PREFIX}recent"
PLAYLIST_NAME = "最近常听"

_TRACKS_TTL_S = 5 * 60.0        # 内存缓存：播放历史变化不频繁，5 分钟足够新
_DISK_TTL_S = 24 * 3600.0       # 磁盘快照：重启恢复与封面反查用
_DB_TIMEOUT_S = 8.0
_MAX_TRACKS = 100
_SQL_LIMIT = 300                # 官方历史取样上限（再按 play_count 截前 N）

_tracks_cache: "dict[str, tuple[list, float]]" = {}   # user_guid -> (tracks, expire)
_tracks_tasks: "dict[str, asyncio.Task]" = {}
_cover_track: "dict[str, str]" = {}                    # user_guid -> 封面曲目 guid


def enabled(conf: "dict | None" = None) -> bool:
    raw = os.environ.get("FNMUSIC_HISTORY_PLAYLIST_ENABLED")
    if raw is None and conf is not None:
        return bool(conf.get("history_playlist_enabled", True))
    if raw is None:
        return True
    return str(raw).strip().lower() in ("true", "1", "yes")


def playlist_size() -> int:
    try:
        return max(1, min(_MAX_TRACKS, int(os.environ.get("FNMUSIC_HISTORY_PLAYLIST_SIZE", "30"))))
    except (TypeError, ValueError):
        return 30


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    return env or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cache_dir() -> str:
    return os.environ.get("FNMUSIC_HISTORY_CACHE_DIR") or os.path.join(home_dir(), "history_cache")


def is_history_playlist_guid(guid: "str | None") -> bool:
    return str(guid or "").startswith(HISTORY_GUID_PREFIX)


def fake_guid(real_guid: str) -> str:
    """与 app.fake_official_guid 同公式（含 "fnmusic-ext::" 盐前缀），改动须两处同步。"""
    return hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()


# --------------------------------------------------------------- 磁盘缓存 --

def _safe_user(user_guid: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "_", str(user_guid or "anon"))[:64] or "anon"


def _disk_path(user_guid: str) -> str:
    return os.path.join(cache_dir(), f"{_safe_user(user_guid)}.json")


def _atomic_write_json(path: str, payload: dict) -> None:
    part = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(part, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(part, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001  缓存写失败不影响下发
        logger.warning("history playlist cache write failed: %s", exc)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def _load_disk(user_guid: str) -> "dict | None":
    try:
        with open(_disk_path(user_guid), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
        return None
    return data


# ------------------------------------------------------------------ 取数 --

def _coerce_ts(v) -> int:
    """play_history.updated_at 是 Go 字符串时间戳；宽松转 epoch 秒，失败 0。"""
    if isinstance(v, (int, float)):
        return int(v)
    text = str(v or "").strip()
    if not text:
        return 0
    try:
        from datetime import datetime, timezone

        text2 = re.sub(r"(\.\d{6})\d+", r"\1", text)  # 纳秒截到微秒
        dt = datetime.fromisoformat(text2.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _norm_key(title: str, artist: str) -> str:
    norm = lambda v: re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】&+/]+", "",
                            str(v or "").lower())  # noqa: E731
    return f"{norm(title)}|{norm(re.split(r'[/&,、]', str(artist or ''))[0])}"


def _read_official_history(db_path: str, user_guid: str) -> list:
    """官方 play_history（含播放次数）。只读，失败返回空。"""
    try:
        from .recommend import user_id_from_guid  # 复用 user_guid→db user.id 映射
    except ImportError:  # 独立加载（测试）时 app 包形态
        from recommend import user_id_from_guid  # type: ignore
    uid = user_id_from_guid(db_path, user_guid)
    if uid is None:
        return []
    sql = """
        SELECT t.guid, t.title,
               (SELECT GROUP_CONCAT(a.name, '/')
                  FROM track_artist ta JOIN artist a ON a.id = ta.artist_id
                 WHERE ta.track_id = t.id) AS artists,
               al.name AS album,
               t.duration_ms, ph.play_count, ph.updated_at
          FROM play_history ph
          JOIN track t ON t.id = ph.track_id
          LEFT JOIN album al ON al.id = t.album_id
         WHERE ph.user_id = ?
         ORDER BY ph.updated_at DESC
         LIMIT ?
    """
    out: list = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=_DB_TIMEOUT_S)
        try:
            rows = con.execute(sql, (uid, _SQL_LIMIT)).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("history playlist: read local play_history failed: %s", exc)
        return []
    for guid, title, artists, album, duration_ms, play_count, updated_at in rows:
        if not guid or not title:
            continue
        out.append({
            "guid": str(guid),
            "title": str(title),
            "artist": str(artists or ""),
            "album": str(album or ""),
            "duration_ms": int(duration_ms or 0),
            "play_count": int(play_count or 1),
            "playedAt": _coerce_ts(updated_at),
            "official": True,
        })
    return out


def _read_online_history(user_guid: str) -> list:
    """代理侧在线播放历史（recency 数据，每曲计 1）。失败返回空。"""
    try:
        from .recommend import load_online_play_history
    except ImportError:  # 独立加载（测试）时 app 包形态
        from recommend import load_online_play_history  # type: ignore
    try:
        items = load_online_play_history(user_guid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history playlist: read online play history failed: %s", exc)
        return []
    if not isinstance(items, list):
        return []
    out: list = []
    for it in items:
        if not isinstance(it, dict):
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        guid = str(it.get("guid") or track.get("guid") or "")
        title = str(track.get("title") or "")
        if not guid or not title:
            continue
        out.append({
            "guid": guid,
            "title": title,
            "artist": str(track.get("artist") or ""),
            "album": str(track.get("album") or ""),
            "duration_ms": int(track.get("duration") or track.get("duration_ms") or 0),
            "play_count": 1,
            "playedAt": int(it.get("playedAt") or 0),
            "official": False,
            "_snapshot": track,
        })
    return out


def _merge_rows(rows: list, limit: int) -> list:
    """按（标题+首歌手）合并同曲，官方条目优先保留；次数与最近播放取最大。"""
    merged: "dict[str, dict]" = {}
    order: list[str] = []
    for row in rows:
        key = _norm_key(row.get("title"), row.get("artist"))
        if not key.strip("|"):
            continue
        prev = merged.get(key)
        if prev is None:
            merged[key] = dict(row)
            order.append(key)
            continue
        prev["play_count"] = max(int(prev.get("play_count") or 1), int(row.get("play_count") or 1))
        prev["playedAt"] = max(int(prev.get("playedAt") or 0), int(row.get("playedAt") or 0))
        # 官方形态优先（播放零跳转、有本地封面）：字段整体换成官方行的
        # （guid/标题/专辑/时长都以官方库为准），仅保留在线侧快照备用
        if row.get("official") and not prev.get("official"):
            snap = prev.get("_snapshot")
            prev.clear()
            prev.update(row)
            prev["official"] = True
            if snap is not None:
                prev.setdefault("_snapshot", snap)
    rows_sorted = sorted(
        (merged[k] for k in order),
        key=lambda r: (-int(r.get("play_count") or 1), -int(r.get("playedAt") or 0)),
    )
    return rows_sorted[:limit]


# ------------------------------------------------------------------ 曲目 --

def build_tracks(user_guid: str, build_track) -> list:
    """同步构建曲目 VO 列表（官方 guid 走官方形态，在线 guid 走在线 VO）。"""
    db_path = ""
    for candidate in _db_path_candidates():
        if os.path.exists(candidate):
            db_path = candidate
            break
    rows = _read_official_history(db_path, user_guid) + _read_online_history(user_guid)
    merged = _merge_rows(rows, playlist_size())
    tracks: list = []
    now = int(time.time())
    for row in merged:
        guid = str(row.get("guid") or "")
        if not guid:
            continue
        try:
            if row.get("official"):
                vo = build_track(guid, {
                    "title": row.get("title"),
                    "artist": row.get("artist"),
                    "album": row.get("album"),
                }, created_at=now)
            else:
                info = dict(row.get("_snapshot") or {})
                info.setdefault("title", row.get("title"))
                info.setdefault("artist", row.get("artist"))
                info.setdefault("album", row.get("album"))
                vo = build_track(guid, info, created_at=now)
        except Exception as exc:  # noqa: BLE001  单曲构造失败只丢这一首
            logger.debug("history playlist track build failed (%s): %s", guid, exc)
            continue
        if isinstance(vo, dict) and vo.get("guid"):
            vo["isFavorite"] = False
            tracks.append(vo)
            _cover_track.setdefault(user_guid, guid)
    return tracks


def _db_path_candidates() -> list:
    """官方 music.db 路径候选：显式 env 优先，其次 recommend 同款默认。"""
    out = []
    try:
        from .recommend import music_db_path
    except ImportError:
        try:
            from recommend import music_db_path  # type: ignore
        except Exception:
            music_db_path = None
    if music_db_path is not None:
        try:
            out.append(music_db_path())
        except Exception:  # noqa: BLE001
            pass
    out.append("/usr/local/apps/@appdata/trim.music/db/music.db")
    seen: set = set()
    return [p for p in out if p and not (p in seen or seen.add(p))]


async def load_tracks(user_guid: str, build_track) -> list:
    """曲目 VO（内存 TTL → 合并任务 → 磁盘快照）。"""
    key = _safe_user(user_guid)
    hit = _tracks_cache.get(key)
    if hit and time.monotonic() < hit[1]:
        return [dict(t) for t in hit[0]]
    existing = _tracks_tasks.get(key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> list:
        # DB 读是同步 sqlite：丢线程池避免阻塞事件循环
        tracks = await asyncio.to_thread(build_tracks, user_guid, build_track)
        _tracks_cache[key] = (tracks, time.monotonic() + _TRACKS_TTL_S)
        _atomic_write_json(_disk_path(user_guid), {
            "version": 1, "savedAt": int(time.time()), "guid": PLAYLIST_GUID,
            "coverTrack": _cover_track.get(user_guid, ""),
            "tracks": tracks,
        })
        return [dict(t) for t in tracks]

    task = asyncio.get_running_loop().create_task(run())
    _tracks_tasks[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        _tracks_tasks.pop(key, None)


def cached_tracks(user_guid: str) -> "list | None":
    """同步取（内存→磁盘），不触发抓取。"""
    key = _safe_user(user_guid)
    hit = _tracks_cache.get(key)
    if hit and time.monotonic() < hit[1]:
        return [dict(t) for t in hit[0]]
    disk = _load_disk(user_guid)
    if disk and time.time() - float(disk.get("savedAt") or 0) < _DISK_TTL_S:
        tracks = disk.get("tracks") or []
        if tracks and disk.get("coverTrack"):
            _cover_track.setdefault(user_guid, str(disk["coverTrack"]))
        return [dict(t) for t in tracks]
    return None


def card_for(user_guid: str, fake_fn=None) -> dict:
    """歌单卡片；trackCount/封面用已缓存曲目数与首个曲目 guid。

    fake_fn 为 app.fake_official_guid（在线封面 guid 伪装成官方 32-hex 并登记
    反查表）；官方真实 guid 原样下发（封面端点本地直出）。"""
    tracks = cached_tracks(user_guid)
    count = len(tracks) if tracks is not None else 0
    cover_track = _cover_track.get(user_guid) or ""
    if not cover_track and tracks:
        cover_track = str((tracks[0] or {}).get("guid") or "")
    cover_id = cover_track
    if cover_id and not re.fullmatch(r"[0-9a-f]{32}", cover_id):
        cover_id = "track_" + (fake_fn(cover_id) if fake_fn else fake_guid(cover_id))
    now = int(time.time())
    return {
        "guid": PLAYLIST_GUID,
        "name": PLAYLIST_NAME,
        "coverId": cover_id or None,
        "createdAt": now,
        "updatedAt": now,
        "trackCount": count,
        "isDaily": True,
    }


def invalidate(user_guid: str = "") -> None:
    """播放事件后失效内存缓存（磁盘快照保留供反查）。"""
    if user_guid:
        _tracks_cache.pop(_safe_user(user_guid), None)
    else:
        _tracks_cache.clear()


def reset_for_test() -> None:
    _tracks_cache.clear()
    _tracks_tasks.clear()
    _cover_track.clear()
