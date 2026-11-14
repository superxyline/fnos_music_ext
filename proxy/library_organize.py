#!/usr/bin/env python3
"""曲库目录整理（二开功能）：按布局移动官方曲库文件，并同步回写 audio_file.path。

与上游 organize.py 引擎的区别（本模块存在的唯一理由）：
organize 引擎只挪文件、官方库靠「重扫」重建路径——重扫会把旧 track 软删、
按新路径重新入库，新 track 意味着新 guid，收藏/歌单/歌词绑定/刮削关联
全部断链。本模块挪完文件后立即在同一事务语义下回写 audio_file.path，
track/guid 行不动，收藏与刮削状态天然保留（做法与 2.3.18 批量改名一致）。

文件搬运/防冲突/事务日志/回滚全部复用 organize._move_group，不重写；
本模块只负责：曲库元数据读取（DB 优先，缺项回落文件标签）→ 目标路径
计算 → 搬运 → DB 路径同步 → 空目录清理。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time

import match_core
import organize as org

logger = logging.getLogger("fnmusic_proxy")

DETAIL_LIMIT = org.DETAIL_LIMIT
DEFAULT_LAYOUT = "artist_album"

_DB_METADATA_SQL = """
    SELECT af.id, af.path, ar.name, al.name, t.is_cue
      FROM track t JOIN audio_file af ON af.id=t.audio_file_id
      LEFT JOIN album al ON al.id=t.album_id
      LEFT JOIN track_artist ta ON ta.track_id=t.id
      LEFT JOIN artist ar ON ar.id=ta.artist_id
     WHERE t.is_audio_file_deleted=0 AND t.is_admin_deleted=0
       AND af.is_physical_file_deleted=0
     ORDER BY ta.artist_order
"""


def _db_library_rows() -> list[dict]:
    """曲库活动文件清单：audio_file.id 去重（cue/重复收录共享同一文件只挪一次）。"""
    conn = match_core._db()
    try:
        rows = conn.execute(_DB_METADATA_SQL).fetchall()
    finally:
        conn.close()
    result: "dict[int, dict]" = {}
    for af_id, path, artist, album, is_cue in rows:
        item = result.get(af_id)
        if item is None:
            item = {"af_id": int(af_id), "path": str(path or ""), "artist": "",
                    "album": "", "is_cue": bool(is_cue)}
            result[af_id] = item
        # cue 行的 artist/album 也算数（父轨可能没挂 artist）
        if str(artist or "").strip() and not item["artist"]:
            item["artist"] = str(artist).strip()
        if str(album or "").strip() and not item["album"]:
            item["album"] = str(album).strip()
    return list(result.values())


def _in_root(path: str, root: str) -> bool:
    """文件是否位于整理根目录内（根外文件绝不移动）。"""
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _first_tag(value) -> str:
    value = getattr(value, "text", value)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _tag_fallback(path: str) -> tuple[str, str]:
    """DB 元数据缺失时回落文件真实标签（484 首信息不全的库，DB 常缺 album）。"""
    try:
        from mutagen import File as MutagenFile
        media = MutagenFile(path, easy=True)
        if media is None:
            return "", ""
        return (_first_tag(media.get("artist") or media.get("TPE1")),
                _first_tag(media.get("album") or media.get("TALB")))
    except Exception as exc:  # 标签坏了/格式不识别——落到「未知歌手」
        logger.debug("library organize tag fallback failed %s: %s", path, exc)
        return "", ""


def _filename_artist(path: str) -> str:
    """老扁平库约定「歌名_歌手.mp3」：DB/标签都没有歌手时从文件名解析。

    取最后一个下划线段（多歌手「A _ B」取 B）；无下划线的「歌手 - 歌名」
    形态不猜（歧义大），返回空。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    if "_" not in stem:
        return ""
    part = stem.split("_")[-1].strip(" _-")
    return part or ""


def build_plan(root: str, layout: str = DEFAULT_LAYOUT,
               items: "list[dict] | None" = None) -> dict:
    """只读规划：不挪任何文件。返回 plan 汇总与逐首明细（截断）。"""
    root = os.path.abspath(root)
    if layout not in org.LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    if items is None:
        items = _db_library_rows()
    plan = {"root": root, "layout": layout, "total": len(items), "toMove": 0,
            "unchanged": 0, "missing": 0, "outside": 0,
            "artists": set(), "albums": set(), "conflicts": [], "missingPaths": [],
            "preview": []}
    for it in items:
        path = it["path"]
        if not path or not os.path.isabs(path):
            plan["missing"] += 1
            continue
        if not os.path.isfile(path):
            plan["missing"] += 1
            plan["missingPaths"].append(path)
            continue
        if not _in_root(path, root):
            plan["outside"] += 1
            continue
        artist, album = it.get("artist") or "", it.get("album") or ""
        if not artist or not album:
            tag_artist, tag_album = _tag_fallback(path)
            artist = artist or tag_artist
            album = album or tag_album
        if not artist:
            artist = _filename_artist(path)
        # 缺信息的归入「未知歌手/未知专辑」目录，不跳过（target_dir 自带该兜底）
        if layout in ("artist", "artist_album") and not artist:
            artist = "未知歌手"
        if layout in ("album", "artist_album") and not album:
            album = "未知专辑"
        target = org.target_dir(root, layout, artist, album)
        plan["artists"].add(os.path.basename(os.path.dirname(target)) if layout in
                            ("artist", "artist_album") else "")
        plan["albums"].add(os.path.basename(target) if layout in
                           ("album", "artist_album") else "")
        if os.path.dirname(path) == target:
            plan["unchanged"] += 1
            continue
        # 目标冲突：同名文件已在目标位置且不是自己（执行时会自动加序号）
        name, ext = os.path.splitext(os.path.basename(path))
        occupied = any(os.path.lexists(os.path.join(target, name + suffix + ext))
                       for suffix in ("", " (2)", " (3)"))
        if occupied:
            plan["conflicts"].append({"path": path, "target": target})
        plan["toMove"] += 1
        if len(plan["preview"]) < 50:
            plan["preview"].append({"path": path, "target":
                                    os.path.join(target, os.path.basename(path)),
                                    "artist": artist, "album": album})
    plan["artists"] = sorted(a for a in plan["artists"] if a)
    plan["albums"] = sorted(a for a in plan["albums"] if a)
    return plan


def _update_db_path(af_id: int, new_path: str) -> None:
    conn = match_core._db()
    try:
        with conn:
            conn.execute("UPDATE audio_file SET path=? WHERE id=?", (new_path, af_id))
    finally:
        conn.close()


# ------- 官方 watcher 标志位对账 -------

RECONCILE_ROUNDS = 4       # 稳定循环轮数上限
RECONCILE_WAIT_S = 15      # 每轮间隔（等官方 watcher 的扫描反应）


def _capture_flags(af_ids: list[int]) -> dict[int, dict]:
    """搬移前采集 audio_file/track 的软删标志（watcher 翻转后的恢复依据）。"""
    if not af_ids:
        return {}
    conn = match_core._db()
    try:
        marks = ",".join("?" * len(af_ids))
        af = {r[0]: r[1] for r in conn.execute(
            f"SELECT id, is_physical_file_deleted FROM audio_file WHERE id IN ({marks})",
            af_ids)}
        tracks: "dict[int, dict]" = {}
        for tid, af_id, a_del, admin in conn.execute(
                f"SELECT id, audio_file_id, is_audio_file_deleted, is_admin_deleted "
                f"FROM track WHERE audio_file_id IN ({marks})", af_ids):
            tracks.setdefault(af_id, {})[tid] = (a_del, admin)
        return {af_id: {"af_del": af.get(af_id, 0), "tracks": tracks.get(af_id, {})}
                for af_id in af_ids}
    finally:
        conn.close()


def _reconcile_flags_once(moved_af_ids: list[int], pre: dict) -> tuple[int, int]:
    """一轮对账：把 watcher 翻转的软删标志恢复到搬移前快照值。

    只恢复本次整理搬过的 audio_file 及其 track，且文件在位才恢复；
    返回 (audio_file 恢复数, track 恢复数)，全 0 表示已稳定。"""
    if not moved_af_ids:
        return 0, 0
    af_fixed = track_fixed = 0
    conn = match_core._db()
    try:
        marks = ",".join("?" * len(moved_af_ids))
        rows = conn.execute(
            f"SELECT id, path, is_physical_file_deleted FROM audio_file "
            f"WHERE id IN ({marks})", moved_af_ids).fetchall()
        for af_id, path, cur_del in rows:
            want = pre.get(af_id, {}).get("af_del", 0)
            if cur_del != want and os.path.isfile(path or ""):
                conn.execute("UPDATE audio_file SET is_physical_file_deleted=? WHERE id=?",
                             (want, af_id))
                af_fixed += 1
        for af_id in moved_af_ids:
            want_tracks = pre.get(af_id, {}).get("tracks", {})
            if not want_tracks:
                continue
            tmarks = ",".join("?" * len(want_tracks))
            for tid, a_del, admin in conn.execute(
                    f"SELECT id, is_audio_file_deleted, is_admin_deleted FROM track "
                    f"WHERE id IN ({tmarks})", list(want_tracks)):
                w_a, w_admin = want_tracks[tid]
                if a_del != w_a or admin != w_admin:
                    conn.execute("UPDATE track SET is_audio_file_deleted=?, "
                                 "is_admin_deleted=? WHERE id=?",
                                 (w_a, w_admin, tid))
                    track_fixed += 1
        conn.commit()
        return af_fixed, track_fixed
    finally:
        conn.close()


async def _reconcile_watcher_flags(moved_af_ids: list[int], pre: dict, job: dict) -> None:
    """整理后的稳定对账循环：官方 watcher 的扫描可能在搬移结束后才反应，
    所以恢复一轮后观察一个间隔，若又被翻转则再恢复，连续两轮零翻转即稳定。"""
    if not moved_af_ids:
        return
    for attempt in range(1, RECONCILE_ROUNDS + 1):
        job["phase"] = f"官方标志对账（第 {attempt}/{RECONCILE_ROUNDS} 轮）"
        af_fixed, track_fixed = await org._worker(
            _reconcile_flags_once, moved_af_ids, pre)
        if af_fixed or track_fixed:
            _detail(job, "reconcile", "restored",
                    f"audio_file={af_fixed}", f"track={track_fixed}")
            logger.info("library organize watcher reconcile round %s: af=%s track=%s",
                        attempt, af_fixed, track_fixed)
        if af_fixed == 0 and track_fixed == 0 and attempt > 1:
            break  # 连续两轮无翻转 → watcher 已稳定
        if attempt < RECONCILE_ROUNDS:
            await asyncio.sleep(RECONCILE_WAIT_S)
    job["phase"] = "整理完成（曲库数据库已同步，无需重扫）"


def _detail(job: dict, category: str, action: str, path: str, reason: str) -> None:
    if len(job["details"]) < DETAIL_LIMIT:
        job["details"].append({"category": category, "action": action,
                               "path": org._limited(path), "reason": org._limited(reason)})


def _error(job: dict, category: str, path: str, exc: Exception) -> None:
    job["failed"] += 1
    if len(job["errors"]) < DETAIL_LIMIT:
        job["errors"].append({"category": category, "path": org._limited(path),
                              "error": org._limited(str(exc))})


async def run_library_organize(runtime, job: dict) -> None:
    """执行整理：搬文件 + 回写 audio_file.path。dryRun 只做规划。"""
    root = os.path.abspath(job["root"])
    layout = job["layout"]
    if layout not in org.LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    job.update(status="running", phase="读取曲库元数据")
    items = await org._worker(_db_library_rows)
    if job.get("dryRun"):
        plan = await org._worker(build_plan, root, layout, items)
        job["plan"] = plan
        job.update(status="completed", phase="规划完成（未改动任何文件）",
                   moved=0, skipped=plan["outside"] + plan["missing"],
                   failed=0)
        job["finished_at"] = time.time()
        return

    org._ensure_target_dir(root, root)
    journal = None
    cache_dir = str(runtime.CONF.get("cache_dir") or "").strip()
    if cache_dir:
        journal = os.path.join(os.path.realpath(os.path.abspath(cache_dir)),
                               "organize-journal", job["id"] + ".jsonl")
        job["journal"] = journal
    try:
        references = await org._worker(org._refs, runtime)
    except ValueError:  # cache_dir 未配置：.ref 引用重写跳过
        references = []
    targets = []
    moved_af_ids: "list[int]" = []
    touched: "set[str]" = set()
    # 搬移前采集官方软删标志：watcher 会把"文件不在旧路径"的行批量标删
    # （2026-11-14 事故实锤），结束后按此快照精确恢复
    pre_flags = await org._worker(_capture_flags, [it["af_id"] for it in items])

    for index, it in enumerate(items):
        path = it["path"]
        job["phase"] = f"整理歌曲 {index + 1}/{len(items)}"
        if not path or not os.path.isfile(path):
            job["skipped"] += 1
            _detail(job, "metadata", "skipped", path, "文件缺失，留在原地")
            continue
        # 🔴 根目录包含性检查：根外文件（其它曲库目录）绝不移动——
        # 与 build_plan 的 outside 口径一致，防止误搬别的目录
        if not _in_root(path, root):
            job["skipped"] += 1
            _detail(job, "outside", "skipped", path, "不在整理根目录内")
            continue
        artist, album = it.get("artist") or "", it.get("album") or ""
        if not artist or not album:
            tag_artist, tag_album = await org._worker(_tag_fallback, path)
            artist = artist or tag_artist
            album = album or tag_album
        if not artist:
            artist = _filename_artist(path)
        # 缺信息归入「未知歌手/未知专辑」（target_dir 自带兜底）
        try:
            target = org.target_dir(root, layout, artist, album)
        except ValueError as exc:
            job["skipped"] += 1
            _detail(job, "metadata", "skipped", path, str(exc))
            continue
        if os.path.dirname(path) == target:
            job["unchanged"] = job.get("unchanged", 0) + 1
            continue
        try:
            if await org._worker(org._busy, runtime, path):
                raise BlockingIOError("文件正被下载、播放或转码占用")
            companions = await org._worker(org._companions, path)

            def move_group():
                with getattr(runtime, "_LIBRARY_FILE_LOCK", None) or org.nullcontext():
                    return org._move_group(path, companions, target, references,
                                           root, journal=journal)
            moved = await org._worker(move_group)
        except BlockingIOError as exc:
            job["skipped"] += 1
            _detail(job, "move", "skipped", path, str(exc))
            continue
        except Exception as exc:
            _error(job, "move", path, exc)
            continue
        # 搬运成功 → 回写 audio_file.path（失败则整体搬回，保持一致）
        new_audio_path = moved[0][1]
        try:
            await org._worker(_update_db_path, it["af_id"], new_audio_path)
        except Exception as exc:
            try:
                source_dir = os.path.dirname(path)
                src_companions = [t for _, t in moved[1:]]

                def move_back():
                    with getattr(runtime, "_LIBRARY_FILE_LOCK", None) or org.nullcontext():
                        return org._move_group(new_audio_path, src_companions,
                                               source_dir, references, root,
                                               journal=journal)
                await org._worker(move_back)
            except Exception as back_exc:
                _error(job, "database", new_audio_path,
                       OSError(f"DB 路径回写失败且搬回也失败，请核对迁移日志: {back_exc}"))
                continue
            _error(job, "database", path, exc)
            continue
        job["moved"] += 1
        targets.append((path, new_audio_path))
        moved_af_ids.append(it["af_id"])
        _detail(job, "move", "moved", path, new_audio_path)
        directory = os.path.dirname(path)
        while directory != root:
            touched.add(directory)
            directory = os.path.dirname(directory)

    job["phase"] = "清理空目录"
    for directory in sorted(touched, key=len, reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    # 🔴 官方 watcher 标志位对账：搬移触发的官方扫描会批量软删（事故实锤），
    # 按搬移前快照恢复并观察稳定，两轮零翻转才算完成
    await _reconcile_watcher_flags(moved_af_ids, pre_flags, job)
    job.update(status="completed", phase="整理完成（曲库数据库已同步，无需重扫）")
    job["finished_at"] = time.time()
    logger.info("library organize done: moved=%s unchanged=%s skipped=%s failed=%s",
                job["moved"], job.get("unchanged", 0), job["skipped"], job["failed"])
