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
import re
import time
from urllib.parse import quote, unquote
from uuid import uuid4

import httpx

logger = logging.getLogger("proxy.publicplaylists")

PUBLIC_GUID_PREFIX = "online:playlist:public:"
_PROVIDERS = ("musicbox", "kugou")
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
        if entry["provider"] == "kugou" and (entry["kind"] != "chart" or entry["id"] not in _KG_CHART_BY_ID):
            continue  # 酷狗只接受内置榜单清单内的 chart 条目
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
    if entry["provider"] == "kugou":
        # 酷狗榜单：取数走酷狗公开接口，播放/收藏走 musicdl 映射源
        return bool(conf.get("musicdl_enabled"))
    if entry["provider"] != "musicbox":
        return False
    if conf.get("netease_enabled"):
        return True
    if conf.get("netease_my_playlists") and _tri(conf.get("netease_playlist_resident")):
        return True
    # 仅公共歌单：webui 会按需把 musicbox 常驻（见 _resident_active），
    # 只有用户显式关掉常驻时才认为不可用。
    return _tri(conf.get("netease_playlist_resident"))


# ------------------------------------------------- 酷狗榜单（第二 provider） --
# 参考 zouclang/fnos_music_ext 的 charts 实现（榜单清单与 m.kugou.com 公开接口），
# 播放不依赖洛雪：曲目经 musicdl 现役源（酷我/咪咕/B站）同名映射后播放，
# 音质门槛/红心自动下载/歌词封面全链路与普通在线曲目一致。

_KG_UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Mobile Safari/537.36"
_KG_META_TTL_S = 4 * 3600.0          # 榜单元数据缓存（酷狗榜每日更新，4h 足够新鲜）
_KG_MAP_DISK_MAX = 4000              # 映射磁盘缓存上限（hash -> musicdl item）

# 酷狗官方榜单清单（rankid 与封面为酷狗公开素材，静态维护）
KG_CHARTS = [
    {"id": "8888", "name": "TOP500", "rankid": 8888, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219164209670219.png"},
    {"id": "6666", "name": "飙升榜", "rankid": 6666, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219193628550054.png"},
    {"id": "74534", "name": "新歌榜", "rankid": 74534, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219200509300768.png"},
    {"id": "52144", "name": "国潮音乐榜", "rankid": 85897, "cover": "https://imge.kugou.com/mcommon/400/20241120/20241120202644296668.jpg"},
    {"id": "100530", "name": "视频号热歌酷狗榜", "rankid": 100530, "cover": "https://imge.kugou.com/mcommon/400/20260701/20260701213145443732.jpg"},
    {"id": "51341", "name": "民谣榜", "rankid": 51341, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211184719475668.jpg"},
    {"id": "59900", "name": "纯音乐榜", "rankid": 59900, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211185324343605.jpg"},
    {"id": "33160", "name": "电音榜", "rankid": 33160, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211184628349205.jpg"},
    {"id": "82831", "name": "网络热歌榜", "rankid": 82831, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219195626644029.png"},
    {"id": "52144b", "name": "短视频热歌榜", "rankid": 52144, "cover": "https://imge.kugou.com/mcommon/400/20260604/20260604150723458910.png"},
    {"id": "59896", "name": "摇滚榜", "rankid": 59896, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211184944114279.jpg"},
    {"id": "24971", "name": "DJ热歌榜", "rankid": 24971, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219194419154116.png"},
    {"id": "80025", "name": "国乐榜", "rankid": 80025, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211184535924138.png"},
    {"id": "85432", "name": "百万收藏榜", "rankid": 85432, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219171042182683.png"},
    {"id": "52767", "name": "短视频收藏人气榜", "rankid": 52767, "cover": "https://imge.kugou.com/mcommon/400/20260702/20260702101133223934.png"},
    {"id": "84235", "name": "名品堂", "rankid": 84235, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219195054422404.png"},
    {"id": "31308", "name": "内地榜", "rankid": 31308, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211192146592398.png"},
    {"id": "33165", "name": "粤语金曲榜", "rankid": 33165, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219200119140556.png"},
    {"id": "31310", "name": "欧美榜", "rankid": 31310, "cover": "https://imge.kugou.com/mcommon/400/20241211/20241211192454699571.jpg"},
    {"id": "51340", "name": "伤感榜", "rankid": 51340, "cover": "https://imge.kugou.com/mcommon/400/20241219/20241219195213930623.png"},
]
_KG_CHART_BY_ID = {str(c["id"]): c for c in KG_CHARTS}

_kg_meta_mem: "dict[str, tuple[list, float]]" = {}       # rankid -> (rows, expire)
_kg_meta_locks: "dict[str, asyncio.Lock]" = {}
_kg_map_mem: "dict[str, dict]" = {}                       # hash -> musicdl item
_kg_map_fail: "dict[str, float]" = {}                     # hash -> 失败时间（当天不再试）
_kg_map_queue: "dict[str, dict]" = {}                     # hash -> meta row（待映射）
_kg_map_task: "asyncio.Task | None" = None
_kg_map_loaded = False


def _kg_norm(v) -> str:
    return re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】&+/]+", "", str(v or "").lower())


def _kg_meta_disk_path() -> str:
    return os.path.join(cache_dir(), "kg_map.json")


def _kg_load_map_disk() -> None:
    global _kg_map_loaded
    if _kg_map_loaded:
        return
    _kg_map_loaded = True
    try:
        with open(_kg_meta_disk_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, dict):
            _kg_map_mem.update({k: v for k, v in items.items() if isinstance(v, dict)})
    except Exception:
        pass


def _kg_save_map_disk() -> None:
    try:
        items = dict(list(_kg_map_mem.items())[-_KG_MAP_DISK_MAX:])
        part = _kg_meta_disk_path() + ".part"
        os.makedirs(os.path.dirname(_kg_meta_disk_path()) or ".", exist_ok=True)
        with open(part, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "savedAt": int(time.time()), "items": items},
                      fh, ensure_ascii=False)
        os.replace(part, _kg_meta_disk_path())
    except Exception as exc:  # noqa: BLE001
        logger.debug("kg map disk save failed: %s", exc)


async def _kg_fetch_meta(client: httpx.AsyncClient, rankid: int, limit: int = 100) -> "tuple[list, str]":
    """酷狗榜单元数据（m.kugou.com 公开接口，自动分页拼到 limit 首）。"""
    tracks: list = []
    cover_url = ""
    seen: set = set()
    page = 1
    while len(tracks) < limit and page <= 5:
        params = {"rankid": rankid, "page": page, "json": "true"}
        try:
            resp = await client.get("http://m.kugou.com/rank/info/", params=params,
                                    headers={"User-Agent": _KG_UA}, timeout=10.0)
            res = resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("kg rank %s page %s fetch failed: %s", rankid, page, exc)
            break
        rows = (res.get("songs") or {}).get("list") or []
        if not rows and page == 1:
            raise RuntimeError(f"kg rank {rankid} 无数据")
        if not cover_url:
            info = res.get("info") or {}
            cov = str(info.get("banner_9") or info.get("banner_7") or info.get("imgurl") or "")
            cover_url = ("https://" + cov[len("http://"):]) if cov.startswith("http://") else cov
        added = 0
        for it in rows:
            if not isinstance(it, dict):
                continue
            fhash = str(it.get("hash") or "")
            if not fhash or fhash in seen:
                continue
            seen.add(fhash)
            singers = it.get("authors") or []
            artist = " / ".join(
                str(a.get("author_name") or "") for a in singers
                if isinstance(a, dict) and a.get("author_name"))
            title = str(it.get("songname") or it.get("filename") or "").strip()
            title = title.replace(f"{artist} - ", "").strip()
            if not title:
                continue
            cov = str(it.get("album_sizable_cover") or "").replace("{size}", "480")
            tracks.append({
                "hash": fhash,
                "title": title,
                "artist": artist,
                "duration_s": int(it.get("duration") or 0),
                "cover_url": ("https://" + cov[len("http://"):]) if cov.startswith("http://") else cov,
            })
            added += 1
            if len(tracks) >= limit:
                break
        if added == 0 or len(rows) < 30:
            break
        page += 1
    return tracks, cover_url


def _kg_pick(items: list, title: str, artist: str) -> "dict | None":
    """musicdl 搜索结果 → 同名最优候选（标题归一相等 + 歌手重叠 + 音质最好）。"""
    t_norm = _kg_norm(title)
    if not t_norm:
        return None
    a_tokens = [_kg_norm(t) for t in re.split(r"[&/,、\s]+", str(artist or "")) if _kg_norm(t)]
    best, best_key = None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        if not str(it.get("download_url") or "").startswith(("http", "/")):
            continue
        if _kg_norm(it.get("title")) != t_norm:
            continue
        cand = _kg_norm(it.get("artist"))
        if a_tokens and cand and not any(t in cand for t in a_tokens):
            continue
        try:
            size = int(it.get("file_size") or 0)
            dur = float(it.get("duration_s") or 0)
        except (TypeError, ValueError):
            size, dur = 0, 0.0
        tier = 0 if str(it.get("ext") or "").lower() in ("flac", "ape", "wav", "aiff") else (
            1 if size > 0 and dur > 0 and size * 8 / dur >= 256_000 else 2)
        key = (tier, str(it.get("source") or ""))
        if best_key is None or key < best_key:
            best, best_key = it, key
    return best


async def _kg_map_one(client: httpx.AsyncClient, row: dict) -> "dict | None":
    """酷狗行 → musicdl 同名最优候选（真实源 guid，播放/收藏/下载全链路复用）。"""
    keyword = f"{row.get('title')} {row.get('artist')}".strip()
    try:
        resp = await client.get(
            f"{os.environ.get('FNMUSIC_MUSICDL_URL', 'http://127.0.0.1:8768')}/search",
            params={"keyword": keyword, "limit": 8}, timeout=40.0)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("kg map search failed for %r: %s", keyword[:32], exc)
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return _kg_pick(items, str(row.get("title")), str(row.get("artist")))


async def _kg_map_worker() -> None:
    """后台映射队列：并发 2、单首间隔 0.6s，映射结果落缓存并重建受影响榜单。"""
    global _kg_map_task
    async with httpx.AsyncClient(trust_env=False) as client:
        sem = asyncio.Semaphore(4)

        async def one(fhash: str, row: dict) -> None:
            async with sem:
                item = await _kg_map_one(client, row)
                if item is not None:
                    _kg_map_mem[fhash] = item
                    _kg_save_map_disk()
                else:
                    _kg_map_fail[fhash] = time.time()
                _kg_map_queue.pop(fhash, None)
                await asyncio.sleep(0.3)

        while _kg_map_queue:
            fhash, row = next(iter(_kg_map_queue.items()))
            await one(fhash, row)
            if not _kg_map_queue:
                break


def _kg_enqueue(rows: list) -> None:
    """未映射行入队并确保后台任务在跑。"""
    global _kg_map_task
    _kg_load_map_disk()
    fresh = False
    for row in rows:
        fhash = str(row.get("hash") or "")
        if not fhash or fhash in _kg_map_mem or fhash in _kg_map_queue:
            continue
        if time.time() - _kg_map_fail.get(fhash, 0.0) < 6 * 3600.0:
            continue
        _kg_map_queue[fhash] = row
        fresh = True
    if fresh and (_kg_map_task is None or _kg_map_task.done()):
        _kg_map_task = asyncio.get_running_loop().create_task(_kg_map_worker())


def _kg_chart_meta(entry: dict) -> "dict | None":
    if entry.get("provider") != "kugou" or entry.get("kind") != "chart":
        return None
    return _KG_CHART_BY_ID.get(str(entry.get("id") or ""))


async def _kg_summary(entry: dict) -> dict:
    meta = _kg_chart_meta(entry)
    if meta is None:
        raise RuntimeError(f"unknown kugou chart {entry.get('id')!r}")
    rows = await _kg_rows_cached(str(meta["rankid"]))
    mapped = sum(1 for r in rows if str(r.get("hash") or "") in _kg_map_mem)
    return {"name": str(meta["name"]), "cover_url": str(meta.get("cover") or ""),
            "track_count": mapped or len(rows)}


async def _kg_rows_cached(rankid: str) -> list:
    hit = _kg_meta_mem.get(rankid)
    if hit and time.monotonic() < hit[1]:
        return hit[0]
    lock = _kg_meta_locks.setdefault(rankid, asyncio.Lock())
    async with lock:
        hit = _kg_meta_mem.get(rankid)
        if hit and time.monotonic() < hit[1]:
            return hit[0]
        async with httpx.AsyncClient(trust_env=False) as client:
            rows, _cover = await _kg_fetch_meta(client, int(rankid), limit=100)
        _kg_meta_mem[rankid] = (rows, time.monotonic() + _KG_META_TTL_S)
        _kg_enqueue(rows)
        return rows


async def _kg_load_tracks(entry: dict, build_track) -> list:
    """酷狗榜单曲目：映射缓存命中的行 → 真实源 VO；未映射行入队后台补。"""
    meta = _kg_chart_meta(entry)
    if meta is None:
        raise RuntimeError(f"unknown kugou chart {entry.get('id')!r}")
    _kg_load_map_disk()
    rows = await _kg_rows_cached(str(meta["rankid"]))
    _kg_enqueue(rows)
    tracks: list = []
    for row in rows:
        item = _kg_map_mem.get(str(row.get("hash") or ""))
        if not isinstance(item, dict):
            continue
        info = {
            "id": str(item.get("id") or ""),
            "source": str(item.get("source") or ""),
            "title": str(row.get("title") or item.get("title") or ""),
            "artist": str(row.get("artist") or item.get("artist") or ""),
            "album": str(item.get("album") or ""),
            "duration_s": float(row.get("duration_s") or item.get("duration_s") or 0),
            "ext": str(item.get("ext") or "mp3"),
            "file_size": item.get("file_size") or 0,
            "cover_url": str(row.get("cover_url") or item.get("cover_url") or ""),
        }
        if not info["id"]:
            continue
        try:
            track = build_track(info)
        except Exception as exc:  # noqa: BLE001
            logger.debug("kg chart track build failed (%s): %s", info["id"], exc)
            continue
        if isinstance(track, dict) and track.get("guid"):
            tracks.append(track)
    return tracks


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
    if entry["provider"] == "kugou":
        summary = await _kg_summary(entry)
        _summary_cache[guid] = (summary, time.monotonic() + _SUMMARY_TTL_S)
        return summary
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
        "needs_vip": row.get("needs_vip") is True,
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
    if entry["provider"] == "kugou":
        tracks = await _kg_load_tracks(entry, build_track)
        _tracks_cache[guid] = (tracks, time.monotonic() + _TRACKS_TTL_S)
        if guid in _tracks_order:
            _tracks_order.remove(guid)
        _tracks_order.append(guid)
        while len(_tracks_order) > _TRACKS_LRU_MAX:
            _tracks_cache.pop(_tracks_order.pop(0), None)
        _kg_save_map_disk()
        return [dict(track) for track in tracks]
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
    _kg_meta_mem.clear()
    _kg_meta_locks.clear()
    _kg_map_queue.clear()
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
