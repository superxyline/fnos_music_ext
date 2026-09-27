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
import sys

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import match_core  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fnmusic_match_gw")

app = FastAPI()


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


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("FNMUSIC_MATCH_PORT", "8776"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
