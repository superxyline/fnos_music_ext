"""口令轮询韧性（2026-11-25 故障回归）。

现象：重新配置后说「播放本地收藏歌单」，桥接再也接管不了任何口令，管理台
也不显示正在播放。

根因链：歌单名匹配 `find_best_match(...)[0]` 返回空列表 → IndexError →
冒到 `run_conversation_loop` → **整个口令轮询任务退出**（且不会自行恢复）。

三道防线：
  A. 歌单名解析兜底（`_safe_list_name`），匹配不到只播报提示；
  B. `do_check_cmd` 吞掉单条口令异常，绝不冒到 poller；
  C. `run_forever` 守护重建轮询（退避，最长 30 秒一次）。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE_PY = (HERE / "native" / "core.py").read_text(encoding="utf-8")


def _func_body(name: str, next_marker: str) -> str:
    start = CORE_PY.index(f"async def {name}(") if f"async def {name}(" in CORE_PY else CORE_PY.index(f"def {name}(")
    return CORE_PY[start:CORE_PY.index(next_marker, start)]


def test_safe_list_name_guards_upstream_indexerror():
    """A：歌单名解析必须兜住上游 IndexError，返回空串而不是抛出。"""
    body = _func_body("_safe_list_name", "async def play_music_list(")
    assert "find_real_music_list_name" in body
    assert "except Exception" in body
    assert "return \"\"" in body
    # 歌单表为空时短路，不去调上游（首次同步未完成/重新配置后）
    assert "music_list" in body and "if not music_list" in body


def test_play_music_list_no_direct_upstream_call():
    """A：两个歌单口令都不许直接调上游匹配（它就是越界源）。"""
    for name, nxt in (("play_music_list", "async def play_favorites_seq("),
                      ("play_music_list_index", "async def select_index(")):
        body = _func_body(name, nxt)
        assert "_safe_list_name(" in body, f"{name} 必须走兜底解析"
        assert "find_real_music_list_name" not in body, f"{name} 不得直调上游"


def test_do_check_cmd_swallows_errors():
    """B：单条口令异常只记日志，不能冒到 poller。"""
    body = _func_body("do_check_cmd", "async def reset_timer_when_answer(")
    assert "try:" in body and "except Exception" in body
    assert "dispatch(" in body
    # dispatch 必须在 try 内
    assert body.index("try:") < body.index("dispatch(")


def test_run_forever_rebuilds_poller():
    """C：轮询异常退出后自动重建，且 CancelledError 必须继续上抛。"""
    body = _func_body("run_forever", "async def cancel_all_tasks(")
    assert "while True" in body, "必须是守护循环"
    assert "run_conversation_loop" in body
    assert "except asyncio.CancelledError" in body and "raise" in body
    assert "backoff" in body and "await asyncio.sleep(backoff)" in body
    assert "min(backoff * 2, 30)" in body, "退避要封顶，避免无限增长"


def test_fallback_to_collect_list_when_name_mismatch():
    """口令名与真实歌单名不一致时，按「收藏」语义回落（唯一候选才猜）。

    实测：「播放本地收藏歌单」而歌单实际叫「收藏歌单」。有多个含「收藏」
    的歌单（乱序/顺序两份）时不猜，交给调用方提示，避免播错歌单。
    """
    body = _func_body("_fallback_collect_list", "async def play_music_list(")
    assert "收藏" in body
    assert "顺序" in body, "必须排除保序歌单"
    assert "len(cands) == 1" in body, "有歧义时不猜"

    safe = _func_body("_safe_list_name", "def _fallback_collect_list(")
    assert "_fallback_collect_list(" in safe, "匹配失败要尝试语义回落"
