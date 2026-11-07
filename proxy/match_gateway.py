#!/usr/bin/env python3
"""歌曲匹配网关（二开功能）：独立 HTTP 入口，供 WebUI 容器转发调用。

由 proxy/run_proxy.sh 后台拉起（与主代理同 cgroup，服务停止时一并被杀）。
监听 0.0.0.0:8776——必须对容器可达（容器经网桥 IP 访问宿主机），与 WebUI
同为「仅限可信内网」定位（README 安全说明），不单独加鉴权。

接口：
  GET  /health
  GET  /api/match/tracks?page=&size=&q=&filter=   曲库列表（含歌词/封面状态）
  POST /api/match/run        {guids:[], wants:["lyric","cover"]} → {taskId}
  GET  /api/match/task/{id}                        任务进度
  GET  /api/match/lyric?guid=                      单曲歌词预览
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import match_core  # noqa: E402
import playlist_import  # noqa: E402  歌单导入引擎（复用同款 .env 加载）
import quality_upgrade  # noqa: E402  曲库音质升级

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic_match_gw")

app = FastAPI()

_MAX_CAND = 50      # 单次找候选上限（每首一次搜索，控制耗时）
_MAX_BATCH_GW = 100  # 单次升级上限


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"code": status, "msg": msg, "data": None}, status_code=200)


@app.get("/health")
async def health():
    return {"ok": True, "service": "fnmusic-match", "sources": match_core._enabled_sources()}


@app.get("/api/match/tracks")
async def tracks(page: int = Query(1), size: int = Query(50),
                 q: str = Query(""), filter: str = Query("all")):
    try:
        return {"code": 0, "msg": "", "data": match_core.list_tracks(page, size, q, filter)}
    except Exception as e:
        logger.exception("list tracks failed")
        return _err(f"列表查询失败: {type(e).__name__}", 500)


@app.get("/api/match/lyric")
async def lyric_preview(guid: str = Query(...)):
    track = match_core._track_row(guid)
    if not track:
        return _err("未找到曲目", 404)
    try:
        text = await asyncio.to_thread(match_core.read_lyric, int(track["id"]))
        return {"code": 0, "msg": "", "data": {"guid": guid, "lyric": text}}
    except Exception as e:
        return _err(f"读取失败: {type(e).__name__}", 500)


@app.post("/api/match/run")
async def run(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    guids = [str(g) for g in (body.get("guids") or []) if g]
    wants = [w for w in (body.get("wants") or []) if w in ("lyric", "cover")]
    if not wants:
        wants = ["lyric", "cover"]
    if not guids:
        return _err("guids 为空")
    if len(guids) > 500:
        return _err("单次最多 500 首")
    # 逐个校验曲目存在（快速失败）
    missing = [g for g in guids if match_core._track_row(g) is None]
    if missing:
        return _err(f"{len(missing)} 个 guid 不存在: {missing[:3]}")
    try:
        task_id = match_core.start_task(guids, wants)
        return {"code": 0, "msg": "", "data": {"taskId": task_id}}
    except RuntimeError:
        return _err("无运行中的事件循环", 500)


@app.get("/api/match/task/{task_id}")
async def task(task_id: str):
    t = match_core.get_task(task_id)
    if not t:
        return _err("任务不存在", 404)
    return {"code": 0, "msg": "", "data": t}


@app.get("/api/match/duplicates")
async def duplicates():
    """扫描重复：按歌名归组 + 组内音质排序（mutagen 读取较慢，放线程池）。"""
    try:
        import asyncio

        data = await asyncio.to_thread(match_core.scan_duplicates)
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        logger.exception("scan duplicates failed")
        return _err(f"扫描失败: {type(e).__name__}", 500)


@app.post("/api/match/dedup")
async def dedup(request: Request):
    """执行去重：items=[{removeTrackId, keepTrackId}]，逐条执行并汇报。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    items = body.get("items") or []
    if not items or not isinstance(items, list):
        return _err("items 为空")
    if len(items) > 200:
        return _err("单次最多 200 项")
    import asyncio

    results = []
    for it in items:
        try:
            rm_id = int(it.get("removeTrackId"))
            kp_id = int(it.get("keepTrackId"))
            res = await asyncio.to_thread(match_core.remove_duplicate, rm_id, kp_id)
            res["ok"] = True
            results.append(res)
        except Exception as e:
            results.append({"removeTrackId": it.get("removeTrackId"), "ok": False,
                            "error": str(e) or type(e).__name__})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"code": 0, "msg": "", "data": {"total": len(results), "ok": ok_n, "results": results}}


# ------------------------------------------------ 曲库音质升级 --

@app.get("/api/upgrade/stats")
async def upgrade_stats():
    try:
        data = await asyncio.to_thread(quality_upgrade.stats)
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        logger.exception("upgrade stats failed")
        return _err(f"统计失败: {type(e).__name__}: {e}", 500)


@app.get("/api/upgrade/tracks")
async def upgrade_tracks(page: int = Query(1), size: int = Query(50),
                         q: str = Query(""), filter: str = Query("all")):
    try:
        data = await asyncio.to_thread(quality_upgrade.list_tracks, page, size, q, filter)
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        logger.exception("upgrade list failed")
        return _err(f"列表查询失败: {type(e).__name__}: {e}", 500)


@app.post("/api/upgrade/candidates")
async def upgrade_candidates(request: Request):
    """批量找候选：只搜索，不下载、不改任何文件。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    guids = [str(g) for g in (body.get("guids") or []) if g]
    if not guids:
        return _err("guids 为空")
    if len(guids) > _MAX_CAND:
        return _err(f"单次最多 {_MAX_CAND} 首，请分批")
    sources = [str(s) for s in (body.get("sources") or []) if s] or None
    mode = "replace" if str(body.get("mode") or "") == "replace" else "upgrade"
    try:
        data = await quality_upgrade.search_candidates(guids, sources, mode=mode)
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        logger.exception("upgrade candidates failed")
        return _err(f"搜索失败: {type(e).__name__}: {e}", 500)


@app.post("/api/upgrade/run")
async def upgrade_run(request: Request):
    """执行升级。dryRun=true 时只下载校验，不替换文件、不写库。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return _err("items 为空")
    if len(items) > _MAX_BATCH_GW:
        return _err(f"单次最多 {_MAX_BATCH_GW} 首")
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        guid = str(it.get("guid") or "")
        cand = it.get("candidate") or {}
        if not guid or not isinstance(cand, dict) or not cand.get("id"):
            continue
        clean.append({"guid": guid, "candidate": cand})
    if not clean:
        return _err("没有有效的升级项（需要 guid + candidate.id）")
    missing = [c["guid"] for c in clean if match_core._track_row(c["guid"]) is None]
    if missing:
        return _err(f"{len(missing)} 个 guid 不存在: {missing[:3]}")
    try:
        mode = "replace" if str(body.get("mode") or "") == "replace" else "upgrade"
        task_id = quality_upgrade.start_task(clean, bool(body.get("dryRun")), mode)
        return {"code": 0, "msg": "", "data": {"taskId": task_id, "total": len(clean),
                                               "dryRun": bool(body.get("dryRun")),
                                               "mode": mode}}
    except RuntimeError:
        return _err("无运行中的事件循环", 500)


@app.get("/api/upgrade/task/{task_id}")
async def upgrade_task(task_id: str):
    t = quality_upgrade.get_task(task_id)
    if not t:
        return _err("任务不存在", 404)
    return {"code": 0, "msg": "", "data": t}


@app.post("/api/manage/delete")
async def manage_delete(request: Request):
    """歌曲管理·物理删除：删文件（含同名 .lrc）+ 删库记录与全部引用，不进回收站。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    guids = [str(g) for g in (body.get("guids") or []) if g]
    if not guids:
        return _err("guids 为空")
    if len(guids) > _MAX_BATCH_GW:
        return _err(f"单次最多 {_MAX_BATCH_GW} 首")
    try:
        data = await asyncio.to_thread(match_core.delete_tracks_physical, guids)
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        logger.exception("manage delete failed")
        return _err(f"删除失败: {type(e).__name__}: {e}", 500)


@app.post("/api/upgrade/rollback")
async def upgrade_rollback(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    guid = str(body.get("guid") or "")
    if not guid:
        return _err("guid 为空")
    try:
        data = await asyncio.to_thread(quality_upgrade.rollback, guid)
    except Exception as e:
        logger.exception("upgrade rollback failed")
        return _err(f"回滚失败: {type(e).__name__}: {e}", 500)
    if not data.get("ok"):
        return _err(str(data.get("error") or "回滚失败"))
    return {"code": 0, "msg": "", "data": data}


@app.get("/api/upgrade/backups")
async def upgrade_backups():
    try:
        return {"code": 0, "msg": "", "data": quality_upgrade.list_backups()}
    except Exception as e:
        return _err(f"读取备份失败: {type(e).__name__}: {e}", 500)


@app.post("/api/upgrade/purge")
async def upgrade_purge(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        data = quality_upgrade.purge_backups(int(body.get("days", 7)))
        return {"code": 0, "msg": "", "data": data}
    except Exception as e:
        return _err(f"清理失败: {type(e).__name__}: {e}", 500)


# ------------------------------------------------ 歌单导入（网易云/QQ） --

@app.get("/api/import/preview")
async def import_preview(request: Request, input: str = Query(..., alias="input"),
                         platform: str = Query("auto")):
    """解析歌单链接/ID，返回歌单名与曲目列表预览（不下载）。"""
    try:
        platform_, pid = playlist_import.parse_input(input, platform)
        data = await playlist_import.fetch_playlist_tracks(platform_, pid)
        return {"code": 0, "msg": "", "data": {"platform": platform_, "id": pid, **data}}
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("import preview failed")
        return _err(f"预览失败: {e}", 502)


@app.get("/api/import/playlists")
async def import_playlists():
    """官方歌单列表（目标歌单下拉）：借小爱向导的飞牛凭据。"""
    client = playlist_import._official_client()
    if client is None:
        return _err("缺少飞牛凭据（设 FNMUSIC_IMPORT_USERNAME/PASSWORD 或完成小爱向导第三步）", 400)
    try:
        await client.login()
        data = await client.api_call("GET", "/playlist/list", query={"page": 1, "size": 200})
        rows = data.get("list") if isinstance(data, dict) else None
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            guid = str(row.get("guid") or "")
            # 代理会往 /playlist/list 注入虚拟歌单（推荐/公共/最近常听），
            # 它们的 add-track 被吸收，不能当导入目标——只留官方 32-hex 歌单
            if not re.fullmatch(r"[0-9a-f]{32}", guid):
                continue
            out.append({"guid": guid,
                        "name": str(row.get("name") or ""),
                        "trackCount": int(row.get("trackCount") or 0)})
        return {"code": 0, "msg": "", "data": out}
    except Exception as e:
        return _err(f"官方歌单拉取失败: {e}", 502)


@app.post("/api/import/run")
async def import_run(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        task_id = await playlist_import.start_import(
            str(body.get("input") or ""), str(body.get("platform") or "auto"),
            str(body.get("targetGuid") or ""), str(body.get("targetNew") or ""),
            int(body.get("limit") or 0))
        return {"code": 0, "msg": "", "data": {"taskId": task_id}}
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:
        logger.exception("import run failed")
        return _err(f"启动失败: {e}", 500)


@app.get("/api/import/task/{task_id}")
async def import_task(task_id: str):
    t = playlist_import.get_task(task_id)
    if not t:
        return _err("任务不存在", 404)
    return {"code": 0, "msg": "", "data": t}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("FNMUSIC_MATCH_PORT", "8776"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
