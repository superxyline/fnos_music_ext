"""点歌兜底回归测试：点歌绝不写入飞牛收藏。

用户反馈（v3.3.4）：口令点一首没收藏的歌——能播，但歌被塞进了 fnOS 收藏。
历史实现（v3.3.0~3.3.4）：搜索命中「本地曲库」结果 → add_favorite → 强制
同步 → 播放；副作用是用户收藏被悄悄污染。

v3.3.5 起：本地曲库命中改为把曲目注入**内存**播放列表
（_inject_to_playlist）——当下可点播、下一首可接续；下次歌单重建
（收藏变化/刷新列表/点歌下载）自动清掉，不落盘、不进收藏。
在线歌仍走 ffmpeg 下载（本来就不入收藏）。
"""
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")


def test_play_fallback_never_favorites():
    """点歌路径不得调用 add_favorite（任何分支）。"""
    assert "add_favorite(" not in APP_PY


def test_resolve_song_injects_local_library_hit():
    """本地曲库命中走内存注入，不再同步/收藏。"""
    body = APP_PY[APP_PY.index("async def _resolve_song("):]
    body = body[:body.index("\ndef _inject_to_playlist(")]
    assert "_inject_to_playlist(" in body
    # 旧实现的两个痕迹都不允许回来
    assert "do_sync(force=True)" not in body
    assert "已收藏以便点播" not in body


def test_inject_updates_memory_only():
    """注入只改内存结构：all_music / music_list / 反查索引，不落盘。"""
    assert "async def _inject_to_playlist" not in APP_PY  # 同步函数
    body = APP_PY[APP_PY.index("def _inject_to_playlist("):]
    body = body[:body.index("\ndef _safe_filename(")]
    assert "lib.all_music[name] = url" in body
    assert "lib.music_list.setdefault(list_name, [])" in body
    assert "save_json" not in body
    assert "fn.add_favorite" not in body


def test_inject_works_on_native_engine():
    """集成：注入后条目与收藏条目同构（http URL），歌名进入目标歌单且幂等。"""
    import importlib

    import app as bridge
    importlib.reload(bridge)

    lib = types.SimpleNamespace(all_music={}, music_list={}, _extra_index_search={})
    bridge.XM = types.SimpleNamespace(music_library=lib)
    bridge.CFG["engine"] = "native"
    bridge.CFG["nas_ip"] = "192.168.x.x"
    bridge.CFG["playlist_name"] = "本地收藏"

    name = bridge._inject_to_playlist("周杰伦 - 晴天", "guid-abc")
    assert name == "周杰伦 - 晴天"
    assert lib.all_music["周杰伦 - 晴天"] == (
        "http://192.168.x.x:8775/play?guid=guid-abc")
    assert "周杰伦 - 晴天" in lib.music_list["本地收藏"]
    assert lib._extra_index_search[
        "http://192.168.x.x:8775/play?guid=guid-abc"] == "周杰伦 - 晴天"

    # 幂等：重复注入不重复追加
    bridge._inject_to_playlist("周杰伦 - 晴天", "guid-abc")
    assert lib.music_list["本地收藏"].count("周杰伦 - 晴天") == 1


def test_inject_ignores_broken_state():
    """没有音乐库/空歌名时安全返回，不抛异常。"""
    import importlib

    import app as bridge
    importlib.reload(bridge)

    bridge.XM = None
    assert bridge._inject_to_playlist("晴天", "guid-x") == ""
    bridge.XM = types.SimpleNamespace(music_library=types.SimpleNamespace(
        all_music={}, music_list={}, _extra_index_search={}))
    assert bridge._inject_to_playlist("", "guid-x") == ""
