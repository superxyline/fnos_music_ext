"""有界异步取链：同键共享任务，调用者取消不取消共享工作（issue #56）。

上游 v2.9.0 用 EAPI 直连重写了取链；本仓库沿用 musicbox 服务已有的进程内
解析（NEMbox），这里补上三条上游收益明确、与协议实现无关的能力：

1. **不阻塞事件循环**：同步 songs_url 调用期间整个服务停摆（其它请求排队），
   解析改走线程池；
2. **同曲同音质并发合并**：首播时 metadata、audio-info、多个 Range 分片几乎
   同时到达，此前各自发起一次解析，白烧额度；现在共用一个任务；
3. **调用者取消不取消共享工作**：某个分片请求断开（App 换歌/重试）不得
   打断仍在为其它等待者服务的解析；单曲总预算 12 秒，超时抛 ResolutionTimeout
   由调用方降级（CLI 兜底或让代理换档）。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("musicbox_service.playback")

# 单曲解析总预算：覆盖一次失败 + 一次重试，但不吃满 App 的起播耐心
BUDGET_S = 12.0

_tasks: "dict[tuple[int, str], asyncio.Task]" = {}


class ResolutionTimeout(Exception):
    """单曲解析超出总预算；调用方应降级（CLI 兜底或换档）。"""


def _discard(key: "tuple[int, str]", task: "asyncio.Task") -> None:
    if _tasks.get(key) is task:
        _tasks.pop(key, None)


def reset() -> None:
    """登录态变化后清空共享任务表（在途任务自然结束，不再被新请求复用）。"""
    _tasks.clear()


def inflight() -> int:
    """当前在途解析任务数（诊断/测试用）。"""
    return len(_tasks)


async def get_song_url_async(song_id: int, quality: str, fresh: bool = False) -> "dict | None":
    """解析单曲直链；同曲同音质并发调用共享同一个解析任务。"""
    from netease_ext import get_song_url

    key = (int(song_id), str(quality))
    task = _tasks.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(
            asyncio.to_thread(get_song_url, int(song_id), str(quality), fresh)
        )
        _tasks[key] = task
        task.add_done_callback(lambda t, k=key: _discard(k, t))
    try:
        # shield：调用者取消（客户端断开）只取消本次等待，共享任务继续为
        # 其它等待者解析；任务完成后结果落在 musicbox 的短缓存里。
        return await asyncio.wait_for(asyncio.shield(task), BUDGET_S)
    except asyncio.TimeoutError as exc:
        raise ResolutionTimeout(f"resolve {song_id} ({quality}) exceeded {BUDGET_S}s") from exc
