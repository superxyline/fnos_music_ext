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
