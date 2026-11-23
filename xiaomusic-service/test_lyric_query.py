"""「搜歌词」口令在音箱端必须被拒（2026-11-23 用户需求）。

搜索框（App/管理台）支持「搜歌词 xxx」按歌词搜；音箱端不支持——口令命中后
直接播报提示，绝不把「搜歌词 xxx」送进搜索链路（否则整串被当歌名搜）。
"""
from pathlib import Path

import pytest

from native.keywords import is_lyric_query

HERE = Path(__file__).resolve().parent
DISPATCH_PY = (HERE / "native" / "dispatch.py").read_text(encoding="utf-8")
CORE_PY = (HERE / "native" / "core.py").read_text(encoding="utf-8")
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("text,expect", [
    ("搜歌词 某人", True),
    ("搜歌词某人", True),
    ("按歌词搜 某人", True),
    ("歌词搜索 某人", True),
    ("歌词搜 某人", True),
    ("  搜歌词 某人", True),
    ("某人", False),
    ("播放晴天", False),
    ("播放搜歌词 某人", False),   # 前缀在中间：由 patched_play 兜底
    ("", False),
])
def test_is_lyric_query(text, expect):
    assert is_lyric_query(text) is expect


def test_dispatch_rejects_lyric_query_first():
    """分发器必须在所有模糊匹配之前拦下，返回专用动作。"""
    body = DISPATCH_PY[DISPATCH_PY.index("def match_cmd(self, device, query, ctrl_panel):"):]
    body = body[:body.index("# -1)")]
    assert "is_lyric_query(query)" in body
    assert "lyric_search_unsupported" in body
    # 必须在睡眠定时/停止/点歌之前（即 match_cmd 的第一段）
    assert body.index("is_lyric_query") < body.index("return") + 200


def test_core_has_unsupported_action_with_tts():
    assert "async def lyric_search_unsupported(" in CORE_PY
    body = CORE_PY[CORE_PY.index("async def lyric_search_unsupported("):]
    body = body[:body.index("async def play_music_list_index(")]
    assert "do_tts" in body
    assert "不支持" in body


def test_play_patch_rejects_lyric_query_in_middle():
    """「播放搜歌词 某人」会被「播放」抢走 → patched_play 必须兜住。"""
    body = APP_PY[APP_PY.index("async def patched_play("):]
    body = body[:body.index("_spawn_auto_playlist_guard(did, cmd_snapshot)")]
    assert "is_lyric_query(name)" in body
    assert "不支持" in body
    # 拦截必须在搜索之前（return 先于 _resolve_song）
    assert body.index("is_lyric_query(name)") < body.index("_resolve_song(")
