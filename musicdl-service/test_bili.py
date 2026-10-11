"""B 站音源（bili_source）测试：标题清洗单测 + API mock 端到端。"""
import pytest

from bili_source import (
    BiliSong,
    clean_text,
    dur_to_s,
    parse_title,
    resolve_audio,
    search_songs,
    search_videos,
)


# ---------------------------------------------------------------- 标题清洗 --
# 飞牛官方刮削按"歌名+歌手"匹配，清洗必须产出正经歌名，视频标题原样落库即废。

@pytest.mark.parametrize("title,keyword,expect", [
    # "歌手 - 歌名"（歌手在前，B 站转载官方 MV 主流形态）
    ("【4K修复】周杰伦 - 晴天", "晴天", ("晴天", "周杰伦")),
    # 花体字符（NFKC 归一）+ 书名号 + 装饰块
    ("【𝐇𝐢-𝐑𝐞无损音质】｜《晴天》- 周杰伦", "晴天", ("晴天", "周杰伦")),
    # 书名号在歌手后
    ("周杰伦《晴天》官方MV", "晴天 周杰伦", ("晴天", "周杰伦")),
    # (Live) 版本注跟随歌名/歌手各种位置
    ("《晴天》- 周杰伦 (Live)", "晴天", ("晴天", "周杰伦")),
    ("晴天 (Live) - 周杰伦", "晴天", ("晴天", "周杰伦")),
    # 英文：keyword 锚定歌名段
    ("Rick Astley - Never Gonna Give You Up", "Never Gonna Give You Up",
     ("Never Gonna Give You Up", "Rick Astley")),
    ("【官方MV】Never Gonna Give You Up - Rick Astley", "never gonna give you up",
     ("Never Gonna Give You Up", "Rick Astley")),
    # 分辨率括号注 + token 平局取短段（歌名）
    ("周杰伦 - 晴天 (4K修复高清)", "晴天 周杰伦", ("晴天", "周杰伦")),
    # 括号外尾注清洗
    ("《孤勇者》- 陈奕迅 1080P无损音质", "孤勇者", ("孤勇者", "陈奕迅")),
    # 无空格连字符
    ("晴天-周杰伦高清MV", "晴天", ("晴天", "周杰伦")),
    # 关键词不在标题开头：前缀为歌手候选
    ("周杰伦 Jay Chou【最新单曲】说好不哭 with 五月天", "说好不哭",
     ("说好不哭 with 五月天", "周杰伦 Jay Chou")),
])
def test_parse_title_official_song_name(title, keyword, expect):
    got = parse_title(title, keyword)
    assert got == expect, f"{title!r} -> {got}, expect {expect}"


def test_parse_title_live_kept_when_no_other_anchor():
    """无关键词锚定时默认"歌手 - 歌名"；段内噪声词被清洗。"""
    got = parse_title("周杰伦 - 晴天", "")
    assert got == ("晴天", "周杰伦")


def test_clean_text_removes_decorations():
    assert clean_text("【4K修复】晴天 高清MV") == "晴天"
    assert clean_text("Ｊａｙ－晴天") == "Jay-晴天"  # NFKC 全角归一
    assert clean_text("") == ""


# ---------------------------------------------------------------- 时长解析 --

def test_dur_to_s():
    assert dur_to_s("4:30") == 270
    assert dur_to_s("1:02:03") == 3723
    assert dur_to_s("") == 0
    assert dur_to_s("bad") == 0


# ---------------------------------------------------------------- API mock --

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _FakeClient:
    """按 URL 前缀路由的假 httpx Client。"""

    def __init__(self, routes):
        self._routes = routes

    def get(self, url, params=None, headers=None):
        for prefix, payload in self._routes.items():
            if url.startswith(prefix):
                return _FakeResp(payload)
        return _FakeResp({"code": -404}, status=404)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _api_payloads():
    search = {"code": 0, "data": {"result": [
        {"bvid": "BV1xx", "title": "【4K修复】周杰伦 - 晴天",
         "author": "JayChouOfficial", "duration": "4:29",
         "pic": "http://i0.hdslb.com/c.jpg"},
        {"bvid": "BV1yy", "title": "晴天 - 翻唱合集 100 首", "author": "x",
         "duration": "1:30:00", "pic": ""},
    ]}}
    view = {"code": 0, "data": {"bvid": "BV1xx", "cid": 123, "duration": 269}}
    play = {"code": 0, "data": {"dash": {"audio": [
        {"id": 30216, "bandwidth": 43962, "baseUrl": "http://upos/64k.m4s"},
        {"id": 30280, "bandwidth": 203786, "baseUrl": "http://upos/192k.m4s"},
    ]}}}
    return {
        "https://api.bilibili.com/x/web-interface/search/type": search,
        "https://api.bilibili.com/x/web-interface/view": view,
        "https://api.bilibili.com/x/player/playurl": play,
    }


def test_search_videos_filters_long_compilation(monkeypatch):
    fake = _FakeClient(_api_payloads())
    monkeypatch.setattr("bili_source.httpx.Client", lambda timeout: fake)
    rows = search_videos("晴天", 5)
    assert len(rows) == 1  # 1:30:00 合集被滤
    assert rows[0]["bvid"] == "BV1xx"
    assert rows[0]["duration_s"] == 269


def test_search_songs_returns_clean_metadata(monkeypatch):
    fake = _FakeClient(_api_payloads())
    monkeypatch.setattr("bili_source.httpx.Client", lambda timeout: fake)
    songs = search_songs("晴天", 3)
    assert len(songs) == 1
    song = songs[0]
    assert isinstance(song, BiliSong)
    # 正经歌名：不是视频标题，飞牛刮削可用
    assert song.song_name == "晴天"
    assert song.singers == "周杰伦"
    assert song.source == "BilibiliMusicClient"
    assert song.identifier == "BV1xx"
    assert song.ext == "m4a"
    # 直链搜索期解析好，且选最高码率音轨
    assert song.download_url == "http://upos/192k.m4s"
    assert song.default_download_headers.get("Referer") == "https://www.bilibili.com/"


def test_search_songs_drops_unresolvable(monkeypatch):
    routes = _api_payloads()
    # BV1xx 解析不出音轨（playurl 空.audio）→ 该条被丢弃
    routes["https://api.bilibili.com/x/player/playurl"] = {
        "code": 0, "data": {"dash": {"audio": []}}}
    fake = _FakeClient(routes)
    monkeypatch.setattr("bili_source.httpx.Client", lambda timeout: fake)
    assert search_songs("晴天", 3) == []


def test_resolve_audio_picks_highest_bandwidth(monkeypatch):
    fake = _FakeClient(_api_payloads())
    monkeypatch.setattr("bili_source.httpx.Client", lambda timeout: fake)
    got = resolve_audio("BV1xx")
    assert got == ("http://upos/192k.m4s", 269)
