"""对账匹配 _match_local_track 单测：歌手占位/顺序分隔/异歌手拒绝。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import _match_local_track  # noqa: E402

TR = [
    {"guid": "e537", "title": "错错错", "artists": [{"name": "陈娟儿"}, {"name": "六哲"}]},
    {"guid": "mt", "title": "错错错", "artists": [{"name": "五月天"}]},
    {"guid": "luo", "title": "错错错", "artists": []},
]


def test_order_and_separator_insensitive():
    """顺序与分隔符不同：六哲&陈娟儿 vs 陈娟儿 六哲 必须命中。"""
    hit = _match_local_track(TR, "错错错", "六哲&陈娟儿")
    assert hit and hit["guid"] == "e537"
    hit = _match_local_track(TR, "错错错", "陈娟儿 / 六哲")
    assert hit and hit["guid"] == "e537"


def test_placeholder_artist_no_constraint():
    """占位歌手不作约束：命中首条（title 相等）。"""
    assert _match_local_track(TR, "错错错", "未知艺术家")["guid"] == "e537"
    assert _match_local_track(TR, "错错错", "")["guid"] == "e537"
    assert _match_local_track(TR, "错错错", "unknown")["guid"] == "e537"


def test_wrong_artist_rejected():
    """歌手 token 不在候选中 → 拒绝该候选，落到无歌手的条目。"""
    hit = _match_local_track(TR, "错错错", "五月天")
    assert hit and hit["guid"] == "mt"
    hit = _match_local_track([TR[0]], "错错错", "周杰伦")
    assert hit is None


def test_title_must_equal():
    assert _match_local_track(TR, "七里香", "五月天") is None
    assert _match_local_track(TR, "错错错 (Live)", "") is None  # 归一化后仍不相等


def test_artist_subset_matches():
    """官方条目歌手不全（仅主唱）vs 下载信息含合作歌手：交集即认同一首。"""
    tr = [{"guid": "g1", "title": "缘分一道桥", "artists": [{"name": "王力宏"}]}]
    assert _match_local_track(tr, "缘分一道桥", "王力宏&谭维维") is tr[0]
    assert _match_local_track(tr, "缘分一道桥", "王力宏") is tr[0]
    # 无交集仍拒绝
    assert _match_local_track(tr, "缘分一道桥", "五月天") is None


def test_prefix_fallback_with_artist_gate():
    """二级前缀匹配（官方 title 带序号/后缀）：歌手交集是防误配闸。"""
    tr = [{"guid": "p1", "title": "03.晴天", "artists": [{"name": "周杰伦"}]}]
    # 前缀 + 歌手交集 → 二级命中
    assert _match_local_track(tr, "晴天", "周杰伦") is tr[0]
    # 歌手不同 → 拦住（防"晴天的约定"式误配）
    assert _match_local_track(tr, "晴天", "五月天") is None
    # 本地无歌手 → 不启用二级（保持严格相等）
    strict = [{"guid": "p2", "title": "晴天的约定", "artists": []}]
    assert _match_local_track(strict, "晴天", "") is None
    assert _match_local_track(strict, "晴天的约定", "") is strict[0]


# ---- 老曲库"文件名整段入库"形态（2026-10-12 用户报红心不落本地的根因）----

FILENAME_TR = [
    # 官方按文件名整段入库：title=完整文件名、track_artist 无关联行
    {"guid": "old1", "title": "黄征 _ 羽·泉 - 奔跑", "artists": []},
    {"guid": "old2", "title": "羽·泉 - 冷酷到底", "artists": []},
]


def test_filename_entry_title_segment_match():
    """title="歌手 - 歌名"整段、artists 空：切段相等 + title 文本做歌手交集命中。"""
    hit = _match_local_track(FILENAME_TR, "奔跑", "黄征 / 羽·泉")
    assert hit and hit["guid"] == "old1"
    hit = _match_local_track(FILENAME_TR, "冷酷到底", "羽·泉")
    assert hit and hit["guid"] == "old2"


def test_filename_entry_wrong_artist_rejected():
    """歌手交集仍把关：官方文件名无该歌手 → 拒绝（防同名误配）。"""
    assert _match_local_track(FILENAME_TR, "奔跑", "周杰伦") is None


def test_filename_entry_partial_artist_intersection():
    """交集即可：快照歌手与文件名写法不同（羽·泉 token 化后交集命中）。"""
    hit = _match_local_track([FILENAME_TR[0]], "奔跑", "羽泉")
    assert hit and hit["guid"] == "old1"


def test_scraped_row_wins_over_filename_row():
    """同曲双行并存：刮削形态行（title=纯歌名）必须优先于文件名形态老行。

    2026-10-12 用户反馈收藏显示"歌手 - 歌名"：段匹配混在第一轮会先命中
    文件名老行——严格相等必须排在段匹配之前。
    """
    BOTH = [
        {"guid": "old", "title": "黄征 _ 羽·泉 - 奔跑", "artists": []},
        {"guid": "new", "title": "奔跑", "artists": [{"name": "黄征"}, {"name": "羽·泉"}]},
    ]
    hit = _match_local_track(BOTH, "奔跑", "黄征 / 羽·泉")
    assert hit and hit["guid"] == "new"
    # 反序（搜索结果顺序不定）：轮1跨候选严格扫描，仍命中刮削行
    hit = _match_local_track(list(reversed(BOTH)), "奔跑", "黄征 / 羽·泉")
    assert hit and hit["guid"] == "new"
