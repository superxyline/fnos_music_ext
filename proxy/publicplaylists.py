"""公共歌单：管理页预选，音乐页注入只读虚拟歌单（FNMUSIC_PUBLIC_PLAYLISTS_*）。

职责：
1. 解析 FNMUSIC_PUBLIC_PLAYLISTS_SELECTION，保留管理员选择顺序；
2. 按已选条目向 musicbox 拉摘要/曲目，统一稳定 guid 与缓存策略；
3. 摘要只读不扫发现池，曲目缓存失败回落 24h 磁盘快照。

设计约束（与本仓库其它注入模块一致）：
- guid 自包含：online:playlist:public:<provider>:<source>:<kind>:<urlencoded id>，
  反解无需查表，重启后客户端持旧 guid 也能定位；
- 卡片 coverId 与 guid 用确定性 md5 伪装成官方 track_+32hex 形态（官方 App 按
  id 格式过滤，裸 online: 不会被渲染成图标）；
- 任何网络异常都只影响该歌单自身（返回已缓存卡片或跳过），绝不影响官方歌单列表；
- 只读：音乐页对这类歌单加歌/移歌/删除均被 proxy 吸收，不回写任何第三方。

当前仅接入网易（musicbox）公共歌单：本仓库的洛雪运行时是自研 Python 实现，
没有可用的发现接口，故 provider 白名单只放 musicbox，数据模型保留 provider
字段以便将来扩展。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from urllib.parse import quote, unquote
from uuid import uuid4

import httpx

logger = logging.getLogger("proxy.publicplaylists")

PUBLIC_GUID_PREFIX = "online:playlist:public:"
_PROVIDERS = ("musicbox",)
_SUMMARY_TTL_S = 30 * 60.0     # 摘要内存缓存
_DISK_TTL_S = 24 * 3600.0      # 磁盘快照有效期（回落与重启恢复窗口）
_FAIL_COOLDOWN_S = 120.0       # 拉取失败冷却：期内直接回落磁盘，不再打 musicbox
_TRACKS_TTL_S = 10 * 60.0      # 曲目内存缓存
_TRACKS_LRU_MAX = 8            # 最多驻留 N 个歌单的曲目
_TRACKS_FETCH_TIMEOUT_S = 60.0
_MAX_TRACKS = 1000
_MAX_SELECTION = 50

# 空选择时的默认注入（网易官方四大榜），与「网易账号歌单」空选择语义保持一致：
# 用户没在管理页动过选择时，至少给出可直接播的榜单，而不是空白。
_DEFAULT_NETEASE = [
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3778678"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "3779629"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "19723756"},
    {"provider": "musicbox", "source": "netease", "kind": "chart", "id": "2884035"},
]

_summary_cache: "dict[str, tuple[dict, float]]" = {}
_summary_fail_until: "dict[str, float]" = {}
_summary_tasks: "dict[str, asyncio.Task]" = {}
_tracks_cache: "dict[str, tuple[list, float]]" = {}
_tracks_fail_until: "dict[str, float]" = {}
_tracks_order: "list[str]" = []
_tracks_tasks: "dict[str, asyncio.Task]" = {}
_cover_urls: "dict[str, str]" = {}
_prefetch_tasks: "set[asyncio.Task]" = set()


def home_dir() -> str:
    env = (os.environ.get("FNMUSIC_HOME") or "").strip()
    return env or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cache_dir() -> str:
    return os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_DIR") or os.path.join(home_dir(), "public_playlists_cache")


# --------------------------------------------------------------- 磁盘缓存 --

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
    except Exception as exc:  # noqa: BLE001  # 缓存写失败不影响在线结果
        logger.warning("public playlist cache write %s failed: %s", path, exc)
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def _load_json(path: str) -> "dict | None":
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    saved = data.get("savedAt")
    if not isinstance(saved, (int, float)) or time.time() - saved >= _DISK_TTL_S:
        return None
    return data


def _digest(guid: str) -> str:
    return hashlib.sha256(guid.encode()).hexdigest()[:24]


def _summary_disk_path(guid: str) -> str:
    return os.path.join(cache_dir(), f"summary-{_digest(guid)}.json")


def _tracks_disk_path(guid: str) -> str:
    return os.path.join(cache_dir(), f"tracks-{_digest(guid)}.json")


# ------------------------------------------------------------ guid 与解析 --

def _entry_key(entry: dict) -> str:
    return "|".join(str(entry.get(k) or "") for k in ("provider", "source", "kind", "id"))


def playlist_guid(entry: dict) -> str:
    provider = str(entry.get("provider") or "")
    source = str(entry.get("source") or "")
    kind = str(entry.get("kind") or "playlist")
    encoded = quote(str(entry.get("id") or ""), safe="")
    return f"{PUBLIC_GUID_PREFIX}{provider}:{source}:{kind}:{encoded}"


def entry_from_guid(guid: str) -> "dict | None":
    raw = str(guid or "")
    if not raw.startswith(PUBLIC_GUID_PREFIX):
        return None
    tail = raw[len(PUBLIC_GUID_PREFIX):]
    parts = tail.split(":", 3)
    if len(parts) != 4 or not all(parts[:3]):
        return None
    provider, source, kind, encoded = parts
    identifier = unquote(encoded)
    if not identifier or kind not in ("playlist", "chart") or provider not in _PROVIDERS:
        return None
    return {"provider": provider, "source": source, "kind": kind, "id": identifier}


def is_public_playlist_guid(guid: "str | None") -> bool:
    return entry_from_guid(str(guid or "")) is not None


def fake_guid(real_guid: str) -> str:
    """与 app.fake_official_guid 同公式（含 "fnmusic-ext::" 盐前缀），改动须两处同步。"""
    return hashlib.md5(f"fnmusic-ext::{real_guid}".encode()).hexdigest()


def _tri(raw) -> bool:
    """三态开关：未配置（None/空串）视为开启，仅显式 false 关闭。"""
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in ("true", "1", "yes")


def parse_selection(raw: object, conf: "dict | None" = None, default_provider: str = "") -> list:
    """选择串 -> 规范化条目列表（保序去重，非法行丢弃）。

    raw 为空串时给默认榜单（管理员从未配置过的首次体验）；显式 "[]" 表示
    用户主动清空，不再回填默认值。
    """
    conf = conf or {}
    text = str(raw if raw is not None else os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", ""))
    text = text.strip()
    if not text:
        rows = list(_DEFAULT_NETEASE)
    else:
        try:
            rows = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("invalid public playlist selection, fallback empty")
            return []
    if not isinstance(rows, list):
        return []
    out: list = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = {
            "provider": str(row.get("provider") or "").strip(),
            "source": str(row.get("source") or "").strip(),
            "kind": str(row.get("kind") or "playlist").strip(),
            "id": str(row.get("id") or "").strip(),
        }
        if entry["provider"] not in _PROVIDERS:
            continue
        if entry["kind"] not in ("playlist", "chart") or not entry["source"] or not entry["id"]:
            continue
        # 网易榜单与同名歌单是同一份内容：去重键统一按 playlist，避免同一榜单出现两次
        key = _entry_key({**entry, "kind": "playlist"})
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= _MAX_SELECTION:
            break
    return out


def configured_selection(conf: dict) -> list:
    if not conf.get("public_playlists_enabled"):
        return []
    return parse_selection(conf.get("public_playlists_selection"), conf)


def _selection_runtime_usable(entry: dict, conf: dict) -> bool:
    """该条目当前是否有可用链路（网易取流需要 musicbox 在场）。"""
    if entry["provider"] != "musicbox":
        return False
    if conf.get("netease_enabled"):
        return True
    if conf.get("netease_my_playlists") and _tri(conf.get("netease_playlist_resident")):
        return True
    # 仅公共歌单：webui 会按需把 musicbox 常驻（见 _resident_active），
    # 只有用户显式关掉常驻时才认为不可用。
    return _tri(conf.get("netease_playlist_resident"))


def _provider_client(app_state, entry: dict) -> httpx.AsyncClient:
    """musicbox 客户端（复用 app.state 上的单例，避免每次请求新建连接池）。"""
    client = getattr(app_state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(
            base_url=os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"), timeout=20.0
        )
        app_state.musicbox_client = client
    return client


def _source_label(source: str) -> str:
    return {
        "netease": "网易",
        "wy": "网易",
        "kw": "酷我",
        "kg": "酷狗",
        "tx": "QQ",
        "mg": "咪咕",
    }.get(source, source.upper())


# --------------------------------------------------------------- 摘要卡片 --

def _card_from_summary(entry: dict, summary: dict) -> dict:
    guid = playlist_guid(entry)
    cover_url = str(summary.get("cover_url") or "")
    if cover_url:
        _cover_urls[guid] = cover_url
    try:
        count = int(summary.get("track_count") or 0)
        updated = int(summary.get("updated_at") or 0)
    except (TypeError, ValueError):
        count, updated = 0, 0
    loaded = cached_tracks(guid)
    if loaded is not None:
        # 已缓存的曲目数是真实可播数，比网易侧计数更准
        count = len(loaded)
    return {
        "guid": guid,
        "name": f"[{_source_label(entry['source'])}] {str(summary.get('name') or entry['id'])}",
        "coverId": "track_" + fake_guid(guid),
        "createdAt": updated or int(time.time()),
        "updatedAt": updated or int(time.time()),
        "trackCount": count,
        # 客户端只对 isDaily 的 online: 歌单拉曲目列表（同热门/每日推荐/账号歌单）
        "isDaily": True,
    }


def _load_disk_summary(guid: str) -> "tuple[dict, dict] | None":
    data = _load_json(_summary_disk_path(guid))
    if not data:
        return None
    entry = entry_from_guid(str(data.get("guid") or guid))
    summary = data.get("summary")
    if entry is None or not isinstance(summary, dict):
        return None
    cover = str(summary.get("cover_url") or "")
    if cover:
        _cover_urls.setdefault(guid, cover)
    return entry, summary


async def _fetch_summary(client: httpx.AsyncClient, entry: dict) -> dict:
    guid = playlist_guid(entry)
    response = await client.get(
        f"/api/v1/public-playlists/{quote(entry['id'], safe='')}",
        params={"source": entry["source"], "kind": entry["kind"]},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"summary http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), dict):
        raise RuntimeError(str((payload or {}).get("error") or "invalid summary payload"))
    summary = payload["data"]
    _summary_cache[guid] = (summary, time.monotonic() + _SUMMARY_TTL_S)
    _atomic_write_json(_summary_disk_path(guid), {
        "version": 1, "savedAt": int(time.time()), "guid": guid, "summary": summary,
    })
    return summary


async def load_summary(app_state, entry: dict) -> "dict | None":
    """歌单卡片（内存 TTL -> 合并请求 -> 磁盘回落）；彻底失败返回 None。"""
    guid = playlist_guid(entry)
    hit = _summary_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return _card_from_summary(entry, hit[0])
    if time.monotonic() < _summary_fail_until.get(guid, 0.0):
        disk = _load_disk_summary(guid)
        return _card_from_summary(entry, disk[1]) if disk else None
    existing = _summary_tasks.get(guid)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> dict:
        try:
            summary = await _fetch_summary(_provider_client(app_state, entry), entry)
            _summary_fail_until.pop(guid, None)
            return _card_from_summary(entry, summary)
        except Exception as exc:  # noqa: BLE001  # 单歌单失败只冷却自己
            _summary_fail_until[guid] = time.monotonic() + _FAIL_COOLDOWN_S
            disk = _load_disk_summary(guid)
            if disk is None:
                logger.info("public playlist summary failed (%s): %s", guid, exc)
                raise
            return _card_from_summary(entry, disk[1])

    task = asyncio.get_running_loop().create_task(run())
    _summary_tasks[guid] = task
    try:
        return await asyncio.shield(task)
    finally:
        _summary_tasks.pop(guid, None)


async def peek_cards(app_state, conf: dict, build_track) -> list:
    """列表注入用：并发取卡片，总预算 2s，慢歌单不拖住已缓存的其它歌单。"""
    entries = [entry for entry in configured_selection(conf) if _selection_runtime_usable(entry, conf)]
    if not entries:
        return []
    tasks = [asyncio.create_task(load_summary(app_state, entry)) for entry in entries]
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    # 后台请求继续完成，慢摘要不会让已经缓存的其他歌单消失。
    for task in pending:
        task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
    cards: list = []
    for entry, task in zip(entries, tasks):
        try:
            result = task.result() if task in done and not task.cancelled() else cached_card(playlist_guid(entry))
        except Exception:  # noqa: BLE001  # 该歌单拉取失败：尝试磁盘快照
            result = cached_card(playlist_guid(entry))
        if isinstance(result, dict):
            cards.append(result)
    if cards:
        schedule_prefetch(app_state, conf, cards, build_track)
    return cards


def cached_card(guid: str) -> "dict | None":
    """同步取已缓存卡片（不触发网络）；未缓存返回 None。"""
    entry = entry_from_guid(guid)
    if entry is None:
        return None
    hit = _summary_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return _card_from_summary(entry, hit[0])
    disk = _load_disk_summary(guid)
    return _card_from_summary(entry, disk[1]) if disk else None


def cover_url_for(guid: str) -> str:
    """歌单封面直链（内存 -> 磁盘摘要）；未知返回空串（封面端点据此 404）。"""
    if guid in _cover_urls:
        return _cover_urls[guid]
    disk = _load_disk_summary(guid)
    return _cover_urls.get(guid, "") if disk else ""


# ------------------------------------------------------------------ 曲目 --

def _musicbox_item(row: dict) -> dict:
    """musicbox 批量详情行 -> build_online_track 入参（形状对齐 recommend 行）。"""
    return {
        "id": f"netease:{row.get('song_id') or row.get('id')}",
        "source": "netease",
        "title": row.get("name") or row.get("song_name"),
        "artist": row.get("artist"),
        "album": row.get("album_name"),
        "duration_ms": row.get("duration_ms") or row.get("duration") or 0,
        "cover_url": row.get("album_pic_url") or row.get("cover_url"),
        "has_sq": row.get("has_sq"),
    }


def _load_disk_tracks(guid: str) -> "list | None":
    data = _load_json(_tracks_disk_path(guid))
    if not data:
        return None
    tracks = data.get("tracks")
    card = data.get("card") if isinstance(data.get("card"), dict) else {}
    cover = str((card or {}).get("cover_url") or "")
    if cover:
        _cover_urls.setdefault(guid, cover)
    return [dict(track) for track in tracks] if isinstance(tracks, list) else None


async def _fetch_tracks(app_state, entry: dict, build_track) -> list:
    guid = playlist_guid(entry)
    client = _provider_client(app_state, entry)
    response = await asyncio.wait_for(client.get(
        f"/api/v1/public-playlists/{quote(entry['id'], safe='')}/tracks",
        params={"source": entry["source"], "kind": entry["kind"]},
        timeout=_TRACKS_FETCH_TIMEOUT_S,
    ), timeout=_TRACKS_FETCH_TIMEOUT_S)
    if response.status_code != 200:
        raise RuntimeError(f"tracks http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), list):
        raise RuntimeError(str((payload or {}).get("error") or "invalid tracks payload"))
    tracks: list = []
    for row in payload["data"][:_MAX_TRACKS]:
        if not isinstance(row, dict):
            continue
        item = _musicbox_item(row) if entry["provider"] == "musicbox" else row
        if not item.get("id"):
            continue
        try:
            track = build_track(item)
        except Exception as exc:  # noqa: BLE001  # 单曲构造失败只丢这一首
            logger.debug("public playlist track build failed (%s): %s", guid, exc)
            continue
        if isinstance(track, dict) and track.get("guid"):
            tracks.append(track)
    _tracks_cache[guid] = (tracks, time.monotonic() + _TRACKS_TTL_S)
    if guid in _tracks_order:
        _tracks_order.remove(guid)
    _tracks_order.append(guid)
    while len(_tracks_order) > _TRACKS_LRU_MAX:
        _tracks_cache.pop(_tracks_order.pop(0), None)
    card = (_summary_cache.get(guid) or ({}, 0))[0]
    _atomic_write_json(_tracks_disk_path(guid), {
        "version": 1, "savedAt": int(time.time()), "guid": guid,
        "card": {**card, "cover_url": _cover_urls.get(guid, "")}, "tracks": tracks,
    })
    return [dict(track) for track in tracks]


async def load_tracks(app_state, entry: dict, build_track) -> list:
    """歌单曲目（VO 形态，real guid）；同歌单并发请求合并为一次抓取。"""
    guid = playlist_guid(entry)
    hit = _tracks_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return [dict(track) for track in hit[0]]
    if time.monotonic() < _tracks_fail_until.get(guid, 0.0):
        disk = _load_disk_tracks(guid)
        if disk is not None:
            return disk
        raise RuntimeError("public playlist tracks recently failed")
    existing = _tracks_tasks.get(guid)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> list:
        try:
            tracks = await _fetch_tracks(app_state, entry, build_track)
            _summary_fail_until.pop(guid, None)
            _tracks_fail_until.pop(guid, None)
            return tracks
        except Exception as exc:  # noqa: BLE001
            _tracks_fail_until[guid] = time.monotonic() + _FAIL_COOLDOWN_S
            disk = _load_disk_tracks(guid)
            if disk is None:
                logger.warning("public playlist tracks failed (%s): %s", guid, exc)
                raise
            logger.info("public playlist tracks fallback to disk (%s): %s", guid, exc)
            return disk

    task = asyncio.get_running_loop().create_task(run())
    _tracks_tasks[guid] = task
    try:
        return await asyncio.shield(task)
    finally:
        _tracks_tasks.pop(guid, None)


def cached_tracks(guid: str) -> "list | None":
    """同步取已缓存曲目（不触发抓取）；未缓存返回 None。"""
    hit = _tracks_cache.get(guid)
    if hit and time.monotonic() < hit[1]:
        return [dict(track) for track in hit[0]]
    return _load_disk_tracks(guid)


def schedule_prefetch(app_state, conf: dict, cards: list, build_track) -> None:
    """列表注入后串行后台预取未缓存歌单曲目（最多 4 个，整批只跑一个任务）。"""
    if any(not task.done() for task in _prefetch_tasks):
        return
    available = {_entry_key(entry) for entry in configured_selection(conf) if _selection_runtime_usable(entry, conf)}
    pending: list[str] = []
    now = time.monotonic()
    by_guid: dict[str, dict] = {}
    for card in cards or []:
        entry = entry_from_guid(str(card.get("guid") or ""))
        if not entry or _entry_key(entry) not in available:
            continue
        guid = playlist_guid(entry)
        by_guid[guid] = entry
        hit = _tracks_cache.get(guid)
        if hit and now < hit[1]:
            continue
        if guid not in pending:
            pending.append(guid)
        if len(pending) >= 4:
            break
    if not pending:
        return

    async def runner() -> None:
        for guid in pending:
            entry = by_guid.get(guid)
            if entry is None:
                continue
            try:
                await load_tracks(app_state, entry, build_track)
            except Exception as exc:  # noqa: BLE001  # 预取失败无所谓，首开再试
                logger.debug("public playlist prefetch failed (%s): %s", guid, exc)

    try:
        task = asyncio.get_running_loop().create_task(runner())
    except RuntimeError:  # 无事件循环（同步上下文调用）
        return
    _prefetch_tasks.add(task)
    task.add_done_callback(_prefetch_tasks.discard)


def reset_for_test() -> None:
    """清空全部进程内状态（测试隔离用）。"""
    _summary_cache.clear()
    _summary_fail_until.clear()
    _tracks_cache.clear()
    _tracks_fail_until.clear()
    _tracks_order.clear()
    _cover_urls.clear()
    for task in (*_summary_tasks.values(), *_tracks_tasks.values(), *_prefetch_tasks):
        if not task.done():
            task.cancel()
    _summary_tasks.clear()
    _tracks_tasks.clear()
    _prefetch_tasks.clear()
