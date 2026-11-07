#!/usr/bin/env python3
"""曲库音质升级（二开功能）。

把本地曲库里已有的歌曲，按「歌名 + 歌手」到指定音源（默认酷我 / 咪咕）找更高音质
的版本，下载后替换原文件。

## 为什么不会破坏收藏和刮削

官方数据结构是三层：

    track（guid / 专辑 / 封面 / 曲目号 / 刮削状态 / 收藏引用）
      └─ track.audio_file_id ─→ audio_file（路径 / 后缀 / 大小 / 码率 / 时长）

收藏挂 `track.guid`，刮削结果（album_id、cover_guid、track_no、metadata_mode、
cloud_scrape_status）也全在 track 表上。本模块**只 UPDATE audio_file 一行**，
track 表一个字段都不动，因此 guid 不变 → 收藏、专辑、封面、曲目号天然保留。

新文件必须写全与原曲目一致的标签，将来官方重扫时元数据才不会被改回文件名。

## 不主动触发重扫

替换后曲库目录里没有"旧路径缺失"，db 里的 path 已指向新文件，状态自洽。
主动触发扫描反而可能因文件哈希变化引发重建，故一律不触发。

## 接口（挂在 match_gateway:8776）

    GET  /api/upgrade/stats                     曲库音质分布
    GET  /api/upgrade/tracks?page&size&q&filter 曲目列表（含真实音质）
    POST /api/upgrade/candidates {guids,sources} 批量找候选（不下载）
    POST /api/upgrade/run {items,dryRun}         执行升级（dryRun=只下载校验不替换）
    GET  /api/upgrade/task/{id}                 任务进度
    POST /api/upgrade/rollback {guid}           回滚单曲
    GET  /api/upgrade/backups                   备份列表
    POST /api/upgrade/purge {days}              清理过期备份
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from typing import Any

import httpx

import match_core

logger = logging.getLogger("fnmusic_upgrade")

# ---------------------------------------------------------------- 常量 --

LOSSLESS_EXT = {"flac", "wav", "ape", "aiff", "aif", "dsf", "dff", "alac"}
LOSSY_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}

# 音质档（越小越好，用于比较"是否更好"）：无损按位深/采样率细分
TIER_LOSSLESS_HIRES = 0   # 24bit 及以上
TIER_LOSSLESS = 1         # 16bit 无损
TIER_HIGH = 2             # ≥256k
TIER_MID = 3              # ≥160k
TIER_LOW = 4              # 其余

# 升级台账：与 repo 同级 sources-data 下（安装时由 install.sh 建目录，容器挂载点同源）
LOG_PATH_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sources-data", "upgrade-log.jsonl")

DEFAULT_SOURCES = ("kuwo", "migu")

_MAX_BATCH = 100
_TASKS: "dict[str, dict]" = {}
_TASK_TTL = 3600.0


# ---------------------------------------------------------------- 工具 --

def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(match_core.MUSIC_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _backup_root() -> str:
    """备份目录。

    **绝不能放在曲库目录内**：官方扫描器并不跳过以 `.` 开头的隐藏目录，放在库里
    会被当成新曲目收录（3.7.3 首次发布的真实事故），进而出现「备份的备份」，
    还会把曲库根判定带偏。默认与升级台账同处 repo 同级的 sources-data 下，
    该目录不在任何共享曲库内，官方扫描不会触碰。
    """
    explicit = _env("FNMUSIC_UPGRADE_BACKUP_ROOT")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(_log_path()), "upgrade-backup")


_LIB_DIRS_TTL = 60.0
_lib_dirs_cache: "tuple[float, set[str]] | None" = None


def _library_dirs() -> "set[str]":
    """官方曲库里实际存在文件的目录集合（安全边界依据）。

    直接用 db 里登记的父目录，而不是猜一个"曲库根"——曲库可以有子目录、
    也可以有多个共享库，取单一路径会误判。
    """
    global _lib_dirs_cache
    now = time.monotonic()
    if _lib_dirs_cache and now - _lib_dirs_cache[0] < _LIB_DIRS_TTL:
        return _lib_dirs_cache[1]
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT path FROM audio_file WHERE is_physical_file_deleted=0").fetchall()
    finally:
        conn.close()
    dirs = {os.path.realpath(os.path.dirname(str(r[0]))) for r in rows if r[0]}
    _lib_dirs_cache = (now, dirs)
    return dirs


def _log_path() -> str:
    return _env("FNMUSIC_UPGRADE_LOG", LOG_PATH_DEFAULT)


def _tier(ext: str, bitrate: int, bits: int = 0) -> int:
    ext = (ext or "").lower().lstrip(".")
    if ext in LOSSLESS_EXT:
        return TIER_LOSSLESS_HIRES if int(bits or 0) >= 24 else TIER_LOSSLESS
    br = int(bitrate or 0)
    if br >= 256000:
        return TIER_HIGH
    if br >= 160000:
        return TIER_MID
    return TIER_LOW


def _tier_label(ext: str, bitrate: int, bits: int = 0) -> str:
    t = _tier(ext, bitrate, bits)
    if t == TIER_LOSSLESS_HIRES:
        return "Hi-Res"
    if t == TIER_LOSSLESS:
        return "无损"
    if t == TIER_HIGH:
        return "高"
    if t == TIER_MID:
        return "中"
    return "低"


def _fmt_bitrate(ext: str, bitrate: int, bits: int = 0, sr: int = 0) -> str:
    """展示用：无损给「采样率/位深」，缺规格时退回估算码率；有损给 kbps。"""
    ext = (ext or "").lower().lstrip(".")
    if ext in LOSSLESS_EXT:
        if sr and bits:
            return f"{sr/1000:.1f}kHz/{bits}bit"
        if bitrate:
            # musicdl 不返回采样率/位深，只能按体积估 —— 标「≈」避免误当成实测值
            return f"无损 ≈{round(int(bitrate)/1000)}kbps"
        return "无损"
    br = int(bitrate or 0)
    if br > 320000:
        # 少数 mp3/m4a 在官方库里记了异常高的码率（VBR 头解析问题），钳到 320k 展示
        br = 320000
    return f"{round(br/1000)}kbps" if br else "未知"


def _audio_probe(path: str) -> dict:
    """mutagen 实测（替换校验用，比 db 字段准）。"""
    info = {"format": os.path.splitext(path)[1].lstrip(".").lower(),
            "lossless": False, "bitrate": 0, "sampleRate": 0, "bitDepth": 0,
            "durationMs": 0, "size": 0, "channels": 0}
    try:
        info["size"] = os.path.getsize(path)
    except OSError:
        return info
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        if audio is None:
            return info
        i = getattr(audio, "info", None)
        if i is not None:
            info["durationMs"] = int(getattr(i, "length", 0) * 1000)
            info["sampleRate"] = int(getattr(i, "sample_rate", 0) or 0)
            info["bitDepth"] = int(getattr(i, "bits_per_sample", 0) or 0)
            info["channels"] = int(getattr(i, "channels", 0) or 0)
            info["bitrate"] = int(getattr(i, "bitrate", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe failed %s: %s", path, exc)
    ext = info["format"]
    info["lossless"] = ext in LOSSLESS_EXT
    if info["lossless"] and info["sampleRate"] and info["bitDepth"]:
        info["bitrate"] = info["sampleRate"] * info["bitDepth"] * max(1, info["channels"])
    return info


# ---------------------------------------------------------------- 列表 --

_LIST_SQL = """
SELECT t.guid AS guid, t.title AS title, t.album_id AS album_id,
       t.cover_guid AS cover_guid, t.track_no AS track_no, t.disc_no AS disc_no,
       t.year AS year, t.metadata_mode AS metadata_mode,
       af.path AS path, af.name AS name, af.suffix AS suffix,
       af.size AS size, af.bitrate AS bitrate, af.sample_rate AS sample_rate,
       af.bit_depth AS bit_depth, af.duration_ms AS duration_ms, af.codec AS codec,
       COALESCE(al.name, '') AS album,
       COALESCE(GROUP_CONCAT(ar.name, ' / '), '') AS artist
FROM track t
JOIN audio_file af ON af.id = t.audio_file_id
LEFT JOIN album al ON al.id = t.album_id
LEFT JOIN track_artist ta ON ta.track_id = t.id
LEFT JOIN artist ar ON ar.id = ta.artist_id
WHERE t.is_admin_deleted = 0 AND t.is_audio_file_deleted = 0
"""


def _filter_clause(filter_: str) -> str:
    lossy = ",".join(f"'{e}'" for e in sorted(LOSSY_EXT))
    lossless = ",".join(f"'{e}'" for e in sorted(LOSSLESS_EXT))
    if filter_ == "lossy":
        return f" AND lower(af.suffix) IN ({lossy})"
    if filter_ == "lossless":
        return f" AND lower(af.suffix) IN ({lossless})"
    if filter_ == "below320":
        return f" AND lower(af.suffix) IN ({lossy}) AND COALESCE(af.bitrate,0) < 300000"
    if filter_ == "hires":
        return f" AND lower(af.suffix) IN ({lossless}) AND COALESCE(af.bit_depth,0) >= 24"
    return ""


def _row_to_item(r: sqlite3.Row) -> dict:
    suffix = str(r["suffix"] or "").lower()
    bits = int(r["bit_depth"] or 0)
    br = int(r["bitrate"] or 0)
    sr = int(r["sample_rate"] or 0)
    lossless = suffix in LOSSLESS_EXT
    path = str(r["path"] or "")
    # 3.7.3 曾把备份放进曲库、被官方扫描收录；这类残留不允许再升级，页面标出来让用户删
    residue = "fnmusic-upgrade-backup" in path
    return {
        "guid": r["guid"],
        "title": r["title"] or "",
        "artist": r["artist"] or "",
        "album": r["album"] or "",
        "path": path,
        "name": r["name"] or "",
        "format": suffix,
        "lossless": lossless,
        "bitrate": br,
        "sampleRate": sr,
        "bitDepth": bits,
        "durationMs": int(r["duration_ms"] or 0),
        "size": int(r["size"] or 0),
        "trackNo": int(r["track_no"] or 0),
        "discNo": int(r["disc_no"] or 0),
        "year": int(r["year"] or 0),
        "quality": _fmt_bitrate(suffix, br, bits, sr),
        "tier": _tier(suffix, br, bits),
        "tierLabel": _tier_label(suffix, br, bits),
        "upgradable": (not residue) and ((not lossless) or bits < 24),
        "residue": residue,
        "hasCover": bool(r["cover_guid"]),
    }


def _probe_meta(path: str) -> dict:
    """读文件内嵌标签（歌曲管理「内嵌信息不全」筛选用）。

    结果按 (mtime, size) 缓存——列表翻页/反复筛选不重复读盘。返回字段：
    exists / title / artist / album / year / trackNo / hasCover。
    """
    try:
        st = os.stat(path)
        cache_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"exists": False, "title": "", "artist": "", "album": "",
                "year": "", "trackNo": "", "hasCover": False}
    cached = _META_CACHE.get(path)
    if cached and cached[0] == cache_key:
        return cached[1]
    meta = {"exists": True, "title": "", "artist": "", "album": "",
            "year": "", "trackNo": "", "hasCover": False}
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        if audio is not None and getattr(audio, "tags", None):
            def _g(*keys: str) -> str:
                for k in keys:
                    try:
                        v = audio.tags.get(k)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v:
                        return str(v[0]) if isinstance(v, (list, tuple)) else str(v)
                return ""
            # ID3（mp3）/ Vorbis Comment（flac/ogg）/ MP4 原子 三套键名各取一份
            meta["title"] = _g("TIT2", "title", "\xa9nam")
            meta["artist"] = _g("TPE1", "artist", "\xa9ART")
            meta["album"] = _g("TALB", "album", "\xa9alb")
            meta["year"] = _g("TDRC", "TYER", "date", "\xa9day")
            meta["trackNo"] = _g("TRCK", "tracknumber", "trkn")
        meta["hasCover"] = bool(_read_embedded_cover(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe meta failed %s: %s", path, exc)
    _META_CACHE[path] = (cache_key, meta)
    return meta


_META_CACHE: "dict[str, tuple]" = {}


def _meta_issues(meta: dict) -> "list[str]":
    """内嵌信息缺口清单（前端展示与筛选判据共用）。"""
    if not meta.get("exists"):
        return ["文件缺失"]
    issues = []
    for field, label in (("title", "标题"), ("artist", "歌手"), ("album", "专辑")):
        if not str(meta.get(field) or "").strip():
            issues.append(label)
    if not meta.get("hasCover"):
        issues.append("封面")
    return issues


def list_tracks(page: int = 1, size: int = 50, keyword: str = "",
                filter_: str = "all") -> dict:
    page = max(1, int(page or 1))
    size = min(max(1, int(size or 50)), 200)
    kw = (keyword or "").strip()
    where = _filter_clause(filter_)
    params: list = []
    if kw:
        esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += (" AND (t.title LIKE ? ESCAPE '\\' OR af.path LIKE ? ESCAPE '\\'"
                  " OR ar.name LIKE ? ESCAPE '\\')")
        params = [f"%{esc}%"] * 3
    if filter_ == "meta_incomplete":
        # 内嵌信息不全：必须逐个读文件真实标签（SQL 层只有 DB 字段），内存过滤分页。
        # 首次全库扫描约几百首 × 每首毫秒级 mutagen 读取，量级可接受；结果有缓存。
        conn = _db()
        try:
            rows = conn.execute(
                f"{_LIST_SQL}{where} GROUP BY t.id ORDER BY t.id DESC", params).fetchall()
        finally:
            conn.close()
        items = []
        for r in rows:
            it = _row_to_item(r)
            issues = _meta_issues(_probe_meta(it["path"]))
            if issues:
                it["metaIssues"] = issues
                items.append(it)
        total = len(items)
        return {"total": total, "page": page, "size": size,
                "items": items[(page - 1) * size:(page - 1) * size + size]}
    conn = _db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM ({_LIST_SQL}{where} GROUP BY t.id) sub)",
            params).fetchone()[0]
        rows = conn.execute(
            f"{_LIST_SQL}{where} GROUP BY t.id ORDER BY t.id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size]).fetchall()
        items = [_row_to_item(r) for r in rows]
        return {"total": total, "page": page, "size": size, "items": items}
    finally:
        conn.close()


def stats() -> dict:
    """曲库音质分布（供页面顶部展示）。"""
    lossy_in = ",".join(f"'{e}'" for e in sorted(LOSSY_EXT))
    lossless_in = ",".join(f"'{e}'" for e in sorted(LOSSLESS_EXT))
    sql = (
        "SELECT lower(af.suffix) AS suffix, af.bitrate AS br, af.bit_depth AS bits, "
        "COUNT(*) AS n FROM track t JOIN audio_file af ON af.id=t.audio_file_id "
        "WHERE t.is_admin_deleted=0 AND t.is_audio_file_deleted=0 "
        "GROUP BY lower(af.suffix), "
        f"CASE WHEN lower(af.suffix) IN ({lossless_in}) THEN af.bit_depth "
        f"WHEN lower(af.suffix) IN ({lossy_in}) THEN CAST(COALESCE(af.bitrate,0)/32000 AS INT) "
        "ELSE 0 END"
    )
    conn = _db()
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    by_format: "dict[str, int]" = {}
    buckets: "dict[str, int]" = {}
    total = 0
    for r in rows:
        n = int(r["n"] or 0)
        total += n
        suf = str(r["suffix"] or "other")
        by_format[suf] = by_format.get(suf, 0) + n
        if suf in LOSSLESS_EXT:
            bits = int(r["bits"] or 0)
            key = "Hi-Res" if bits >= 24 else "无损"
        else:
            key = _fmt_bitrate(suf, int(r["br"] or 0))
        buckets[key] = buckets.get(key, 0) + n
    return {"total": total, "byFormat": by_format, "buckets": buckets}


# ---------------------------------------------------------------- 候选 --

_NORM_STRIP = re.compile(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】]+")


def _norm_text(s: Any) -> str:
    return _NORM_STRIP.sub("", str(s or "").lower())


def _artist_tokens(artist: str) -> "list[str]":
    return [t for t in (_norm_text(x) for x in re.split(r"[&/,、\s]+", str(artist or ""))) if t]


def _cand_bitrate(item: dict, duration_s: float) -> int:
    """候选码率：musicdl 只给文件大小与时长，按 size*8/duration 估算（无损也估）。"""
    size = int(item.get("file_size") or 0)
    if size and duration_s > 0:
        return int(size * 8 / duration_s)
    return 0


_BRACKET_TAIL = re.compile(r"[\(（\[【].*?[\)）\]】]\s*$")


def _base_title(s: Any) -> str:
    """去掉末尾括号补充后归一（「嗜好 (回忆版)」→「嗜好」）。"""
    return _norm_text(_BRACKET_TAIL.sub("", str(s or "")))


def _shape_candidate(item: dict, cur_item: dict, mode: str = "upgrade") -> "dict | None":
    """musicdl 搜索条目 → 前端候选。

    分两档：
      exact   —— 标题归一相等 + 歌手重叠，可放心升级；
      suspect —— 标题相同但歌手对不上，或「原曲名 + 版本后缀」，只作参考，
                 默认不勾选，由用户在候选列表里自行判断。
    完全不相干的直接丢弃（宁可漏，不可错配）。

    mode="replace"（歌曲管理·换源）：本地元数据可能本来就是错的（歌手不符
    很常见），suspect 不降权丢弃照常返回；额外计算 infoScore（候选自身信息
    完整度：标题/歌手吻合、专辑、封面、时长吻合），前端按它综合音质排序。
    """
    if not isinstance(item, dict):
        return None
    cid = str(item.get("id") or "")
    if not cid:
        return None
    title = str(item.get("title") or "")
    cur_title = cur_item.get("title")
    c_norm, t_norm = _norm_text(title), _norm_text(cur_title)
    if not t_norm:
        return None
    artist = str(item.get("artist") or "")
    toks = _artist_tokens(cur_item.get("artist"))
    artist_ok = (not toks) or (not artist) or any(t in _norm_text(artist) for t in toks)

    if c_norm == t_norm:
        kind = "exact" if artist_ok else "suspect"
        reason = "" if artist_ok else "歌手不符"
    elif _base_title(title) == _base_title(cur_title) and artist_ok:
        kind = "suspect"
        reason = "版本后缀不同"
    else:
        return None

    ext = str(item.get("ext") or "mp3").lower()
    dur_s = float(item.get("duration_s") or 0)
    br = _cand_bitrate(item, dur_s)
    bits = 24 if (ext in LOSSLESS_EXT and
                  int(item.get("sample_rate") or 0) > 48000) else 0
    tier = _tier(ext, br, bits)
    cur_dur = int(cur_item.get("durationMs") or 0)
    dur_diff = abs(dur_s * 1000 - cur_dur) if (dur_s and cur_dur) else 0
    better = (kind == "exact") and tier < int(cur_item.get("tier") or TIER_LOW)

    # 信息完整度评分（0~6）：换源模式的核心排序依据——本地信息错的歌，
    # 要优先选「音源里信息更全/更吻合」的候选来修标签。
    info_score = 0
    if c_norm == t_norm:
        info_score += 2
    elif _base_title(title) == _base_title(cur_title):
        info_score += 1
    if toks and artist and any(t in _norm_text(artist) for t in toks):
        info_score += 1
    if str(item.get("album") or "").strip():
        info_score += 1
    if str(item.get("cover_url") or "").strip():
        info_score += 1
    if dur_s and cur_dur and dur_diff <= 3000:
        info_score += 1
    elif not cur_dur:
        info_score += 0  # 本地时长未知，无法判断，不加分

    return {
        "id": cid,
        "source": str(item.get("source") or ""),
        "title": title,
        "artist": artist,
        "album": str(item.get("album") or ""),
        "format": ext,
        "bitrate": br,
        "size": int(item.get("file_size") or 0),
        "durationS": round(dur_s, 1),
        "quality": _fmt_bitrate(ext, br, bits, 0),
        "tier": tier,
        "tierLabel": _tier_label(ext, br, bits),
        "kind": kind,
        "reason": reason,
        "better": better,
        "infoScore": info_score,
        "durationDiffMs": int(dur_diff),
        "durationSuspicious": bool(dur_diff and dur_diff > 3000),
        "coverUrl": str(item.get("cover_url") or ""),
    }


_CAND_CACHE: "dict[str, dict]" = {}   # musicdl id → 候选原始条目（含 download_url）


async def _search_one(http: httpx.AsyncClient, item: dict, sources: "list[str]",
                      mode: str = "upgrade") -> dict:
    out = {"guid": item["guid"], "title": item.get("title"), "artist": item.get("artist"),
           "current": {k: item.get(k) for k in
                       ("format", "bitrate", "sampleRate", "bitDepth", "quality", "tier",
                        "durationMs", "size")},
           "candidates": [], "error": ""}

    async def _query(kw: str) -> "list[dict]":
        try:
            resp = await http.get(f"{match_core.MUSICDL_URL}/search",
                                  params={"keyword": kw, "limit": 20,
                                          "sources": ",".join(sources)}, timeout=45)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.info("upgrade search failed %r: %s", kw, exc)
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    title = str(item.get("title") or "")
    artist = str(item.get("artist") or "")
    raw = await _query(f"{title} {artist}".strip())

    def _collect(rows: "list[dict]", seen: "set[str]") -> "list[dict]":
        got = []
        for it in rows:
            c = _shape_candidate(it, item, mode)
            if not c or c["id"] in seen:
                continue
            seen.add(c["id"])
            got.append(c)
            _CAND_CACHE[c["id"]] = it
        return got

    seen: "set[str]" = set()
    cands = _collect(raw, seen)
    # 「歌名 歌手」召回不足时，补一次纯歌名（酷我/咪咕对「歌名+歌手」有时只给同歌手其它歌）
    if not any(c["kind"] == "exact" for c in cands):
        cands += _collect(await _query(title), seen)

    if not cands:
        out["error"] = "音源里没有这首歌"
        return out
    if mode == "replace":
        # 换源：信息完整度优先，其次音质档位/体积；时长可疑压底防错配
        cands.sort(key=lambda c: (0 if c["kind"] == "exact" else 1,
                                  1 if c["durationSuspicious"] else 0,
                                  -c["infoScore"], c["tier"], -c["size"]))
    else:
        cands.sort(key=lambda c: (0 if c["kind"] == "exact" else 1,
                                  0 if c["better"] else 1,
                                  c["tier"], -c["size"]))
    out["mode"] = mode
    out["candidates"] = cands
    out["exactCount"] = sum(1 for c in cands if c["kind"] == "exact")
    return out


async def search_candidates(guids: "list[str]", sources: "list[str] | None" = None,
                            concurrency: int = 4, mode: str = "upgrade") -> dict:
    srcs = [s.strip() for s in (sources or list(DEFAULT_SOURCES)) if s.strip()]
    if not srcs:
        srcs = list(DEFAULT_SOURCES)
    items = []
    for g in guids:
        it = get_track_item(g)
        if it:
            items.append(it)
    sem = asyncio.Semaphore(max(1, min(8, concurrency)))
    results: "list[dict]" = []

    async with httpx.AsyncClient() as http:
        async def _run(it: dict) -> None:
            async with sem:
                results.append(await _search_one(http, it, srcs, mode))

        await asyncio.gather(*[_run(it) for it in items])
    order = {g: i for i, g in enumerate(guids)}
    results.sort(key=lambda r: order.get(r["guid"], 999))
    return {"sources": srcs, "items": results}


def get_track_item(guid: str) -> "dict | None":
    conn = _db()
    try:
        r = conn.execute(f"{_LIST_SQL} AND t.guid = ? GROUP BY t.id", (guid,)).fetchone()
        return _row_to_item(r) if r else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 标签 --

def _read_embedded_cover(path: str) -> bytes:
    try:
        from mutagen import File as MutagenFile
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4

        audio = MutagenFile(path)
        if isinstance(audio, FLAC) and audio.pictures:
            return audio.pictures[0].data
        if isinstance(audio, MP4):
            covr = audio.tags.get("covr") if audio.tags else None
            if covr:
                return bytes(covr[0])
        try:
            tags = ID3(path)
            for k in tags.keys():
                if k.startswith("APIC"):
                    return tags[k].data
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("read cover failed %s: %s", path, exc)
    return b""


def _write_tags_full(path: str, ext: str, meta: dict, cover: bytes, lyric: str) -> None:
    """写入与原曲目一致的标签，保证将来官方重扫不会改动元数据。"""
    ext = (ext or "").lower()
    title = str(meta.get("title") or "")
    artist = str(meta.get("artist") or "")
    album = str(meta.get("album") or "")
    try:
        if ext == "mp3":
            from mutagen.id3 import (APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK,
                                     USLT, ID3, ID3NoHeaderError)
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TIT2"); tags.delall("TPE1"); tags.delall("TALB")
            tags.delall("TRCK"); tags.delall("TPOS"); tags.delall("TDRC")
            tags.delall("APIC"); tags.delall("USLT")
            if title:
                tags.add(TIT2(encoding=3, text=title))
            if artist:
                tags.add(TPE1(encoding=3, text=artist))
                tags.add(TPE2(encoding=3, text=artist))
            if album:
                tags.add(TALB(encoding=3, text=album))
            if meta.get("track_no"):
                tags.add(TRCK(encoding=3, text=str(meta["track_no"])))
            if meta.get("disc_no"):
                tags.add(TPOS(encoding=3, text=str(meta["disc_no"])))
            if meta.get("year"):
                tags.add(TDRC(encoding=3, text=str(meta["year"])))
            if cover:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            if lyric:
                tags.add(USLT(encoding=3, lang="chi", desc="", text=lyric))
            tags.save(path, v2_version=4)
            return
        if ext == "flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(path)
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            if album:
                audio["album"] = album
            if meta.get("track_no"):
                audio["tracknumber"] = str(meta["track_no"])
            if meta.get("disc_no"):
                audio["discnumber"] = str(meta["disc_no"])
            if meta.get("year"):
                audio["date"] = str(meta["year"])
            if lyric:
                audio["lyrics"] = lyric
            if cover:
                audio.clear_pictures()
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover
                audio.add_picture(pic)
            audio.save()
            return
        if ext in ("m4a", "mp4", "m4b"):
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = [artist]
                audio["aART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            if meta.get("track_no"):
                audio["trkn"] = [(int(meta["track_no"]), int(meta.get("track_total") or 0))]
            if meta.get("disc_no"):
                audio["disk"] = [(int(meta["disc_no"]), 0)]
            if meta.get("year"):
                audio["\xa9day"] = [str(meta["year"])]
            if lyric:
                audio["\xa9lyr"] = [lyric]
            if cover:
                audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return
        # 其它格式：easy 兜底
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"写标签失败: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- 执行 --

def _sniff_ext(path: str) -> str:
    """按文件头判断真实容器，避免扩展名与实际格式不一致。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(16)
    except OSError:
        return ""
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    if head[:4] == b"MAC ":
        return "ape"
    return ""


def _append_log(entry: dict) -> None:
    path = _log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("write upgrade log failed: %s", exc)


def _read_log() -> "list[dict]":
    path = _log_path()
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return []
    return out


def _ensure_in_library(path: str) -> None:
    """安全边界：只允许操作曲库目录内的文件（防误删/误覆盖）。

    判据是该文件的父目录必须出现在官方 db 登记的目录里；备份目录天然不在其中，
    所以对备份文件的操作会被挡下（避免「备份的备份」）。
    """
    real = os.path.realpath(path)
    parent = os.path.dirname(real)
    if parent not in _library_dirs():
        raise RuntimeError(f"路径不在曲库内，拒绝操作: {path}")


async def _download(http: httpx.AsyncClient, cand_id: str, dest: str,
                    nominal_s: float = 0.0) -> "tuple[int, float]":
    async with http.stream("GET", f"{match_core.MUSICDL_URL}/stream",
                           params={"id": cand_id, "proxy": "true"}, timeout=300) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"下载失败 HTTP {resp.status_code}")
        length = resp.headers.get("content-length", "")
        expected = int(length) if length.isdigit() else None
        written = 0
        with open(dest, "wb") as fp:
            async for chunk in resp.aiter_bytes(64 * 1024):
                if chunk:
                    fp.write(chunk)
                    written += len(chunk)
    if written < 512 * 1024:
        raise RuntimeError(f"文件过小({written}B)，可能是试听片段")
    if expected is not None and written != expected:
        raise RuntimeError(f"下载不完整 {written}/{expected}")
    probe = _audio_probe(dest)
    actual_s = probe.get("durationMs", 0) / 1000.0
    if nominal_s and actual_s and actual_s < nominal_s * 0.6:
        raise RuntimeError(f"疑似试听片段 {actual_s:.0f}s / 标称 {nominal_s:.0f}s")
    return written, actual_s


async def _upgrade_one(http: httpx.AsyncClient, guid: str, cand: dict,
                       dry_run: bool, mode: str = "upgrade") -> dict:
    item = get_track_item(guid)
    if not item:
        return {"guid": guid, "ok": False, "error": "曲目不存在"}
    old_path = item["path"]
    bak_root = os.path.realpath(_backup_root())
    if os.path.realpath(old_path).startswith(bak_root + os.sep):
        return {"guid": guid, "ok": False,
                "error": "该曲目指向升级备份文件（不是曲库曲目），请先把它从曲库移除"}
    _ensure_in_library(old_path)
    if not os.path.exists(old_path):
        return {"guid": guid, "ok": False, "error": "原文件不存在"}
    if not cand or not cand.get("id"):
        return {"guid": guid, "ok": False, "error": "候选无效"}

    old_probe = _audio_probe(old_path)
    old_dur_ms = int(item.get("durationMs") or old_probe.get("durationMs") or 0)

    target_dir = os.path.dirname(old_path)
    stem = os.path.splitext(os.path.basename(old_path))[0]
    tmp = os.path.join(target_dir, f".upgrade-{uuid.uuid4().hex}.part")

    tx = None
    try:
        nominal_s = old_dur_ms / 1000.0
        size, actual_s = await _download(http, str(cand["id"]), tmp, nominal_s)
        sniffed = _sniff_ext(tmp)
        new_ext = sniffed or str(cand.get("format") or "").lower() or "mp3"
        if new_ext == "mp4":
            new_ext = "m4a"
        if new_ext not in (LOSSLESS_EXT | LOSSY_EXT):
            raise RuntimeError(f"不支持的格式: {new_ext}")
        probe = _audio_probe(tmp)
        dur_ms = int(probe.get("durationMs") or 0)
        if old_dur_ms and dur_ms and abs(dur_ms - old_dur_ms) > 3000:
            raise RuntimeError(f"时长不符（原 {old_dur_ms/1000:.1f}s / 新 {dur_ms/1000:.1f}s），疑似错配")
        new_tier = _tier(new_ext, probe.get("bitrate") or 0, probe.get("bitDepth") or 0)
        if mode != "replace" and new_tier >= int(item.get("tier") or TIER_LOW):
            raise RuntimeError(f"音质未提升（{_fmt_bitrate(new_ext, probe.get('bitrate') or 0, probe.get('bitDepth') or 0, probe.get('sampleRate') or 0)}）")

        cover = _read_embedded_cover(old_path)
        if not cover and cand.get("coverUrl"):
            try:
                r = await http.get(str(cand["coverUrl"]), timeout=20)
                if r.status_code == 200 and len(r.content) > 1024:
                    cover = r.content
            except Exception:  # noqa: BLE001
                pass
        lyric = ""
        try:
            tid = _track_id(guid)
            if tid:
                lyric = await asyncio.to_thread(match_core.read_lyric, tid) or ""
        except Exception:  # noqa: BLE001
            lyric = ""
        meta = {"title": item["title"], "artist": item["artist"], "album": item["album"],
                "track_no": item.get("trackNo"), "disc_no": item.get("discNo"),
                "year": item.get("year")}
        if not item.get("artist"):
            meta["artist"] = cand.get("artist") or ""
        if mode == "replace":
            # 换源模式的核心诉求就是修元数据：候选的信息来自音源（比本地内嵌可靠），
            # 用户手动选定后以候选为准写标签；候选缺哪项回落本地值。
            meta["title"] = str(cand.get("title") or "").strip() or item["title"]
            meta["artist"] = str(cand.get("artist") or "").strip() or item["artist"]
            meta["album"] = str(cand.get("album") or "").strip() or item["album"]
        _write_tags_full(tmp, new_ext, meta, cover, lyric)

        result = {
            "guid": guid, "ok": True, "dryRun": bool(dry_run), "mode": mode,
            "title": item["title"], "artist": item["artist"],
            "from": {"format": item["format"], "quality": item["quality"],
                     "size": item["size"], "path": old_path},
            "to": {"format": new_ext,
                   "quality": _fmt_bitrate(new_ext, probe.get("bitrate") or 0,
                                           probe.get("bitDepth") or 0,
                                           probe.get("sampleRate") or 0),
                   "size": size, "bitrate": probe.get("bitrate") or 0,
                   "sampleRate": probe.get("sampleRate") or 0,
                   "bitDepth": probe.get("bitDepth") or 0,
                   "durationMs": dur_ms, "source": cand.get("source") or "",
                   "path": os.path.join(target_dir, f"{stem}.{new_ext}")},
        }
        if dry_run:
            os.remove(tmp)
            result["note"] = "试运行：已校验未替换"
            return result

        # ---- 真替换 ----
        new_path = os.path.join(target_dir, f"{stem}.{new_ext}")
        _ensure_in_library(new_path)
        backup_dir = _backup_root()
        os.makedirs(backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(backup_dir, f"{stamp}__{os.path.basename(old_path)}")
        shutil.move(old_path, backup_path)

        try:
            os.replace(tmp, new_path)
        except Exception:
            shutil.move(backup_path, old_path)   # 还原
            raise

        try:
            _update_audio_file(_audio_file_id(guid), new_path, new_ext, size, probe)
        except Exception:
            # db 更新失败 → 文件级回滚，保持一致
            try:
                os.replace(new_path, tmp)
                shutil.move(backup_path, old_path)
            except Exception as exc2:  # noqa: BLE001
                logger.error("rollback after db failure ALSO failed: %s", exc2)
            raise

        _append_log({
            "ts": int(time.time()), "guid": guid, "title": item["title"],
            "artist": item["artist"], "mode": mode,
            "oldPath": old_path, "oldFormat": item["format"],
            "newPath": new_path, "newFormat": new_ext,
            "backup": backup_path, "dbId": _audio_file_id(guid),
            "old": {"format": item["format"], "size": item["size"],
                    "bitrate": item["bitrate"], "sampleRate": item["sampleRate"],
                    "bitDepth": item["bitDepth"], "durationMs": item["durationMs"]},
            "new": {"size": size, "bitrate": probe.get("bitrate") or 0,
                    "sampleRate": probe.get("sampleRate") or 0,
                    "bitDepth": probe.get("bitDepth") or 0, "durationMs": dur_ms},
        })
        return result
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _track_id(guid: str) -> "int | None":
    conn = _db()
    try:
        r = conn.execute("SELECT id FROM track WHERE guid=?", (guid,)).fetchone()
        return int(r["id"]) if r else None
    finally:
        conn.close()


def _audio_file_id(guid: str) -> int:
    conn = _db()
    try:
        r = conn.execute("SELECT audio_file_id FROM track WHERE guid=?", (guid,)).fetchone()
        if not r:
            raise RuntimeError("曲目不存在")
        return int(r["audio_file_id"])
    finally:
        conn.close()


def _update_audio_file(af_id: int, new_path: str, new_ext: str, size: int,
                       probe: dict) -> None:
    """只更新 audio_file 一行；track 表完全不动（收藏/刮削因此保留）。"""
    codec = {"flac": "flac", "mp3": "mp3", "m4a": "aac", "wav": "pcm_s16le",
             "ape": "ape", "ogg": "vorbis"}.get(new_ext, new_ext)
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE audio_file SET path=?, name=?, suffix=?, size=?, "
            "duration_ms=?, bitrate=?, sample_rate=?, bit_depth=?, container=?, "
            "codec=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_path, os.path.basename(new_path), new_ext, int(size),
             int(probe.get("durationMs") or 0), int(probe.get("bitrate") or 0),
             int(probe.get("sampleRate") or 0), int(probe.get("bitDepth") or 0),
             new_ext, codec, int(af_id)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 任务 --

def _prune() -> None:
    now = time.monotonic()
    for k, t in list(_TASKS.items()):
        if now - t["createdAt"] > _TASK_TTL and not t.get("running"):
            _TASKS.pop(k, None)


def start_task(items: "list[dict]", dry_run: bool, mode: str = "upgrade") -> str:
    _prune()
    task_id = uuid.uuid4().hex[:12]
    _TASKS[task_id] = {
        "id": task_id, "items": list(items), "dryRun": bool(dry_run),
        "mode": "replace" if mode == "replace" else "upgrade",
        "results": [], "running": True, "createdAt": time.monotonic(),
        "current": "", "okCount": 0, "failCount": 0,
    }
    asyncio.get_running_loop().create_task(_run_task(task_id))
    return task_id


async def _run_task(task_id: str) -> None:
    t = _TASKS.get(task_id)
    if not t:
        return
    async with httpx.AsyncClient() as http:
        for entry in t["items"]:
            guid = str(entry.get("guid") or "")
            cand = entry.get("candidate") or {}
            t["current"] = guid
            try:
                res = await _upgrade_one(http, guid, cand, t["dryRun"], t.get("mode") or "upgrade")
            except Exception as exc:  # noqa: BLE001
                logger.warning("upgrade %s failed: %s", guid, exc)
                res = {"guid": guid, "ok": False, "error": str(exc) or type(exc).__name__}
            t["results"].append(res)
            if res.get("ok"):
                t["okCount"] += 1
            else:
                t["failCount"] += 1
    t["running"] = False
    t["current"] = ""
    logger.info("upgrade task %s done: ok=%d fail=%d", task_id, t["okCount"], t["failCount"])


def get_task(task_id: str) -> "dict | None":
    t = _TASKS.get(task_id)
    if not t:
        return None
    return {"id": task_id, "total": len(t["items"]), "done": len(t["results"]),
            "running": t["running"], "current": t["current"],
            "dryRun": t["dryRun"], "okCount": t["okCount"], "failCount": t["failCount"],
            "results": t["results"]}


# ---------------------------------------------------------------- 回滚 --

def rollback(guid: str) -> dict:
    entries = [e for e in _read_log() if e.get("guid") == guid]
    if not entries:
        return {"ok": False, "error": "没有该曲目的升级记录"}
    e = entries[-1]
    cur = get_track_item(guid)
    if not cur:
        return {"ok": False, "error": "曲目不存在"}
    backup = str(e.get("backup") or "")
    old_path = str(e.get("oldPath") or "")
    if not backup or not os.path.exists(backup):
        return {"ok": False, "error": f"备份文件不存在: {backup}"}
    _ensure_in_library(old_path)
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = e.get("old") or {}
        conn.execute(
            "UPDATE audio_file SET path=?, name=?, suffix=?, size=?, duration_ms=?, "
            "bitrate=?, sample_rate=?, bit_depth=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (old_path, os.path.basename(old_path),
             str(e.get("oldFormat") or cur["format"]), int(old.get("size") or 0),
             int(old.get("durationMs") or 0), int(old.get("bitrate") or 0),
             int(old.get("sampleRate") or 0), int(old.get("bitDepth") or 0),
             int(e.get("dbId") or _audio_file_id(guid))))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"db 回滚失败: {exc}"}
    finally:
        conn.close()
    new_path = str(e.get("newPath") or cur["path"])
    try:
        if os.path.exists(new_path) and os.path.realpath(new_path) != os.path.realpath(old_path):
            os.remove(new_path)
        shutil.move(backup, old_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"文件回滚失败: {exc}（db 已回滚）"}
    _append_log({"ts": int(time.time()), "guid": guid, "rollback": True,
                 "title": e.get("title"), "path": old_path})
    return {"ok": True, "path": old_path}


# ---------------------------------------------------------------- 备份 --

def list_backups() -> dict:
    root = _backup_root()
    if not os.path.isdir(root):
        return {"dir": root, "files": [], "count": 0, "size": 0}
    files = []
    total = 0
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        total += st.st_size
        files.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"dir": root, "files": files, "count": len(files), "size": total}


def purge_backups(days: int = 7) -> dict:
    days = max(0, int(days if days is not None else 7))
    root = _backup_root()
    if not os.path.isdir(root):
        return {"removed": 0, "kept": 0}
    cutoff = time.time() - days * 86400
    removed, kept = 0, 0
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.isfile(p) and os.stat(p).st_mtime < cutoff:
                os.remove(p)
                removed += 1
            else:
                kept += 1
        except OSError:
            kept += 1
    return {"removed": removed, "kept": kept, "days": days}
