"""测试全局守卫：默认禁用真 ffmpeg，防止用例意外转码真实在线源。

需要验证转码路径的用例自行 monkeypatch transcode.FFMPEG_BIN 指向假实现
（tests 内置 fake_ffmpeg 脚本），其余用例一律走无 ffmpeg 的回落分支。
"""
import pytest

from proxy import transcode as tc


@pytest.fixture(autouse=True)
def _no_real_ffmpeg(monkeypatch):
    monkeypatch.setattr(tc, "FFMPEG_BIN", None)
    monkeypatch.setattr(tc, "FFPROBE_BIN", None)
    # 隔离会话注册表与并发槽，避免跨用例串扰
    monkeypatch.setattr(tc, "_SESSIONS", {})
    monkeypatch.setattr(tc, "_ACTIVE", 0)
    monkeypatch.setattr(tc, "_SLOT_COND", None)


@pytest.fixture(autouse=True)
def _isolate_cover_state(monkeypatch):
    """隔离封面 guid→直链 映射与 32hex 反解取证去重表（模块级内存状态，
    不隔离会让先跑用例的映射让后续用例跳过 mock 音源、甚至真实出网）。"""
    from proxy import app as pa

    monkeypatch.setattr(pa, "_COVER_GUID_URL_MEM", {})
    monkeypatch.setattr(pa, "_COVER_UNRESOLVED_SEEN", set())


@pytest.fixture(autouse=True)
def _isolate_netease_resolution(monkeypatch):
    """隔离网易直链钉链与共享取链注册表。

    钉链按 song_id 全局记账：不隔离时先跑用例钉住的 URL 会让后续「全部失败应
    返回 None」的用例直接命中旧钉链，表现为凭空的成功结果。
    """
    from proxy import app as pa

    monkeypatch.setattr(pa, "_NETEASE_URL_PIN", {})
    monkeypatch.setattr(pa, "_NETEASE_URL_INFLIGHT", {})
    monkeypatch.setattr(pa, "_NETEASE_PREFETCH", set())
    monkeypatch.setattr(pa, "_NETEASE_PREHEATED_PINS", {})
    monkeypatch.setattr(pa, "_NETEASE_URL_CONFIG", None)


@pytest.fixture(autouse=True)
def _isolate_lyric_search(monkeypatch):
    """歌词搜索（3.8.0）默认隔离：本地分片目录指空 + 在线歌词源关闭。

    聚合搜索的歌词源懒建网易公开接口 client 且会以候选歌名二跳复调音源，
    不隔离会让搜索类用例凭空多一个源（出网/计数/时长全被扰动）；歌词
    专项用例（test_lyric_search.py）自行打开对应 CONF 项。"""
    from proxy import app as pa

    monkeypatch.setitem(pa.CONF, "lyric_sqlite_dir", "")
    monkeypatch.setitem(pa.CONF, "lyric_online_enabled", False)
