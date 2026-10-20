"""audio_id  resolver 回归测试（不依赖 xiaomusic 环境）。

防护目标（2026-10-20 用户反馈"带屏小爱播放界面偶尔显示错误"）：
- X4B 等设备屏幕元数据（歌名/歌手/封面/歌词）由 audio_id 的曲库记录决定，
  上游 _get_audio_id 按「歌名-歌手」拆名与本桥接「歌手 - 歌名」条目名格式
  错位 → 只能掉 songList[0] 兜底（蒙对/蒙错=偶尔显示错误）；
- 修复面：双格式拆名 + 标题规范化 + 三级匹配 + 预解析缓存 + 负缓存；
- 回归红线：接口异常（登录失效）不得写负缓存（否则登录恢复后永远 miss）。
"""

import asyncio
from pathlib import Path

import pytest

from audio_id_resolver import (
    AudioIdCache,
    DEFAULT_AUDIO_ID,
    gen_query_candidates,
    match_audio_id,
    normalize_text,
    resolve_audio_ids,
    search_audio_id,
    strip_tags,
)

HERE = Path(__file__).resolve().parent
APP_PY = (HERE / "app.py").read_text(encoding="utf-8")
RESOLVER = (HERE / "audio_id_resolver.py").read_text(encoding="utf-8")


class FakeMina:
    """假 mina_service：query → songList（或异常）。记录调用次数。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def mina_request(self, path, params):
        self.calls.append((path, params.get("query")))
        r = self.responses.get(params.get("query"))
        if isinstance(r, BaseException):
            raise r
        return {"data": {"songList": r if isinstance(r, list) else []}}


def _song(name, artist="周杰伦", audio_id="111"):
    return {"name": name, "artist": {"name": artist}, "audioID": audio_id}


# ---------------- 规范化 ----------------

def test_normalize_text_fullwidth_and_space():
    assert normalize_text("稻　香  feat. 袁咏琳") == "稻 香 feat. 袁咏琳"


def test_normalize_text_t2s(tmp_path):
    """繁→简归一：飞牛元数据繁体（如许嵩"身體/毀/時間/賞"）必须能对上小米曲库简体。

    opencc 是容器必装依赖；本地无该包时此用例跳过（转换降级跳过，不影响功能）。
    """
    pytest.importorskip("opencc", reason="需要 opencc-python-reimplemented")
    assert normalize_text("許嵩") == "许嵩"
    assert normalize_text("早睡身體好") == "早睡身体好"
    assert normalize_text("雅俗共賞") == "雅俗共赏"
    # 繁体歌名对简体曲库条目可命中（match_audio_id 内部走 normalize_text）
    songs = [_song("蝴蝶的时间", "许嵩", "111")]
    assert match_audio_id(songs, "蝴蝶的時間", "許嵩") == "111"


def test_normalize_text_t2s_degrades_without_opencc(monkeypatch):
    """无 opencc 环境必须降级跳过（不能崩）——模拟 import 失败。"""
    import audio_id_resolver as r
    monkeypatch.setattr(r, "_CC", False)   # False = 不可用哨兵
    assert r.normalize_text("早睡身體好") == "早睡身體好"  # 原样返回，不抛异常


def test_strip_tags_removes_version_suffix():
    assert strip_tags("稻香 (Live)") == "稻香"
    assert strip_tags("稻香（feat. 袁咏琳）") == "稻香"
    assert strip_tags("稻香 - Remaster 2020") == "稻香"
    # 无修饰时原样返回
    assert strip_tags("稻香") == "稻香"


# ---------------- 候选查询 ----------------

def test_gen_query_candidates_bridge_format_first():
    """桥接 build_playlist 的格式是「歌手 - 歌名」，必须作为第一候选。"""
    cands = gen_query_candidates("周杰伦 - 稻香")
    assert cands[0][:2] == ("稻香", "周杰伦")   # title, artist
    assert "周杰伦 稻香" in [c[2] for c in cands]


def test_gen_query_candidates_filename_format_supported():
    """点歌注入/本地文件名常见「歌名 - 歌手」，互换后应能命中。"""
    cands = gen_query_candidates("稻香 - 周杰伦")
    pairs = {c[:2] for c in cands}
    assert ("稻香", "周杰伦") in pairs
    assert ("周杰伦", "稻香") in pairs


def test_gen_query_candidates_no_dash_and_dedup():
    cands = gen_query_candidates("稻香")
    assert len(cands) == 1
    assert cands[0][:2] == ("稻香", "")
    # 歌手/歌名同名只产出一个候选
    cands = gen_query_candidates("周杰伦 - 周杰伦")
    assert len(cands) == 2  # 互换去重后仅一对 + 整体兜底


def test_gen_query_candidates_empty():
    assert gen_query_candidates("") == []
    assert gen_query_candidates(None) == []


# ---------------- 匹配 ----------------

def test_match_audio_id_strict():
    songs = [_song("不能说的秘密", "周杰伦", "222"), _song("稻香", "周杰伦", "111")]
    assert match_audio_id(songs, "稻香", "周杰伦") == "111"


def test_match_audio_id_loose_when_artist_mismatch():
    """歌手对不上也认——屏幕至少歌名对（歌声是桥接 URL，不受影响）。"""
    songs = [_song("稻香", "周杰伦", "111")]
    assert match_audio_id(songs, "稻香", "其他歌手") == "111"


def test_match_audio_id_multi_artist_separators():
    songs = [_song("稻香", "周杰伦;蔡依林", "111")]
    assert match_audio_id(songs, "稻香", "周杰伦/蔡依林") == "111"


def test_match_audio_id_fuzzy_contains():
    """近似互含：曲库名带括号修饰（strip 不掉的后缀）。"""
    songs = [_song("稻香 (合唱版)", "周杰伦", "111")]
    assert match_audio_id(songs, "稻香", "周杰伦") == "111"
    songs2 = [_song("稻香", "周杰伦", "222")]
    assert match_audio_id(songs2, "稻香 合唱版", "周杰伦") == "222"


def test_match_audio_id_no_false_positive():
    songs = [_song("爱我别走", "张震岳", "333")]
    assert match_audio_id(songs, "爱", "张震岳") is None  # 单字防误配
    assert match_audio_id(songs, "晴天", "周杰伦") is None
    assert match_audio_id([], "稻香", "周杰伦") is None


# ---------------- 搜索 ----------------

def test_search_audio_id_first_candidate_wins():
    mina = FakeMina({"周杰伦 稻香": [_song("稻香", "周杰伦", "111")]})
    aid = asyncio.run(search_audio_id(mina, "周杰伦 - 稻香", interval=0))
    assert aid == "111"
    assert len(mina.calls) == 1  # 第一候选命中，不再多打


def test_search_audio_id_second_candidate():
    mina = FakeMina({
        # 第一候选（桥接格式解析：歌名=稻香）结果里没有目标歌
        "周杰伦 稻香": [_song("无关的歌", "某人", "xxx")],
        # 互换解析（歌名=周杰伦）命中
        "稻香 周杰伦": [_song("周杰伦", "稻香", "222")],
    })
    aid = asyncio.run(search_audio_id(mina, "周杰伦 - 稻香", interval=0))
    assert aid == "222"
    assert len(mina.calls) == 2


def test_search_audio_id_not_found():
    mina = FakeMina({"q": []})
    aid = asyncio.run(search_audio_id(mina, "查无此歌", interval=0))
    assert aid == ""


def test_search_audio_id_api_error_propagates():
    """接口异常必须向上抛（上层据此中止预解析、不写负缓存）。"""
    mina = FakeMina({"周杰伦 稻香": RuntimeError("token 失效")})
    with pytest.raises(RuntimeError):
        asyncio.run(search_audio_id(mina, "周杰伦 - 稻香", interval=0))


# ---------------- 缓存 ----------------

def test_cache_roundtrip_and_negative(tmp_path):
    path = tmp_path / "sub" / "audio_id_cache.json"
    cache = AudioIdCache(path)
    assert cache.get("A") is None
    cache.set("A", "111")
    cache.set("B", "")            # 负缓存
    cache.set("A", "111")          # 同值不重复写
    cache.save()
    # 重启后重载
    reloaded = AudioIdCache(path)
    assert reloaded.get("A") == "111"
    assert reloaded.get("B") == ""
    assert reloaded.get("C") is None
    assert reloaded.stats() == {"resolved": 1, "negative": 1}


def test_cache_prune(tmp_path):
    path = tmp_path / "audio_id_cache.json"
    cache = AudioIdCache(path)
    for n in ("A", "B", "C"):
        cache.set(n, "111")
    removed = cache.prune(["A", "D"])
    assert removed == 2
    assert cache.get("A") == "111"
    assert cache.get("B") is None


def test_cache_corrupt_file_falls_back_empty(tmp_path):
    path = tmp_path / "audio_id_cache.json"
    path.write_text("not json", encoding="utf-8")
    assert AudioIdCache(path).stats() == {"resolved": 0, "negative": 0}


# ---------------- 批量预解析 ----------------

def test_resolve_audio_ids_stats_and_negative(tmp_path):
    mina = FakeMina({
        "周杰伦 稻香": [_song("稻香", "周杰伦", "111")],
        # 查无此歌各候选都空
    })
    cache = AudioIdCache(tmp_path / "c.json")
    stats = asyncio.run(resolve_audio_ids(
        mina, ["周杰伦 - 稻香", "查无此歌"], cache, interval=0))
    assert stats["hit"] == 1 and stats["miss"] == 1 and stats["aborted"] == 0
    assert cache.get("周杰伦 - 稻香") == "111"
    assert cache.get("查无此歌") == ""   # 曲库没有 → 负缓存


def test_resolve_audio_ids_abort_on_api_error_no_negative(tmp_path):
    """登录失效/风控时接口异常：中止本轮且不写负缓存（登录恢复后重试）。"""
    mina = FakeMina({"周杰伦 稻香": RuntimeError("token 失效")})
    cache = AudioIdCache(tmp_path / "c.json")
    stats = asyncio.run(resolve_audio_ids(mina, ["周杰伦 - 稻香"], cache, interval=0))
    assert stats["aborted"] == 1
    assert cache.get("周杰伦 - 稻香") is None  # 不是 ""（没有负缓存）


def test_resolve_audio_ids_incremental(tmp_path):
    """已缓存条目不再打接口（增量预解析）。"""
    mina = FakeMina({"周杰伦 稻香": [_song("稻香", "周杰伦", "111")]})
    cache = AudioIdCache(tmp_path / "c.json")
    asyncio.run(resolve_audio_ids(mina, ["周杰伦 - 稻香"], cache, interval=0))
    calls_after_first = len(mina.calls)
    asyncio.run(resolve_audio_ids(mina, ["周杰伦 - 稻香", "新歌 - 新人"],
                                  cache, interval=0))
    # 只有未缓存的"新歌 - 新人"打了接口（两个候选互换 + 整体兜底 = 3 次）
    assert len(mina.calls) == calls_after_first + 3


# ---------------- 结构性回归（app.py 集成面） ----------------

def test_app_integrates_audio_id_resolver():
    """lifespan 注册 + do_sync 触发预解析 + patch 覆盖上游方法，缺一不可。"""
    assert "from audio_id_resolver import" in APP_PY, "app.py 未引入 resolver"
    assert "patch_audio_id_resolver()" in APP_PY, "lifespan 必须注册 audio_id patch"
    assert "_spawn_audio_id_resolve(playlist)" in APP_PY, "do_sync 必须触发预解析"
    assert "XiaoMusicDevice._get_audio_id = patched" in APP_PY, "patch 未覆盖上游"
    assert "AUDIO_ID_CACHE_FILE = BRIDGE_DIR /" in APP_PY, "缓存落盘路径缺失"
    assert '"audio_id_cache": AUDIO_ID_CACHE.stats()' in APP_PY, "/health 未暴露缓存"


def test_patch_shortcircuits_non_play_music_devices():
    """非 play_music 路径设备不得打搜索接口（照搬上游短路）。"""
    assert "hardware in NEED_USE_PLAY_MUSIC_API" in APP_PY
    assert "return fixed" in APP_PY
    # 短路分支必须用上游默认值语义
    assert "DEFAULT_AUDIO_ID" in APP_PY
