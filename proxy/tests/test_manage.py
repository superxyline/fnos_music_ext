"""歌曲管理单测：物理删除（文件+库记录+引用）与换源候选的信息完整度评分。

只测纯逻辑与临时模拟库；替换链路的文件操作靠真机试运行验证（同音质升级）。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import match_core  # noqa: E402
import quality_upgrade as qu  # noqa: E402
from test_match import SCHEMA  # noqa: E402  复用同一份官方库模拟 schema


@pytest.fixture()
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    meta = tmp_path / "meta"
    (meta / "lyric-sqlite").mkdir(parents=True)
    (meta / "cover").mkdir(parents=True)
    db = tmp_path / "music.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    # 真实官方库有 metadata_mode / bitrate 等列（_LIST_SQL 会选），补齐以免 list_tracks 报错
    conn.execute("ALTER TABLE track ADD COLUMN metadata_mode INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE audio_file ADD COLUMN bitrate INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE audio_file ADD COLUMN sample_rate INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE audio_file ADD COLUMN bit_depth INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE audio_file ADD COLUMN codec TEXT DEFAULT ''")
    conn.execute("INSERT INTO audio_file (id, path, name) VALUES (1, ?, 'a.mp3')",
                 (str(lib / "后弦 - 单车恋人.mp3"),))
    conn.execute("INSERT INTO audio_file (id, path, name) VALUES (2, ?, 'b.mp3')",
                 (str(lib / "无歌词.mp3"),))
    conn.execute("INSERT INTO artist (id, name) VALUES (1, '后弦')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (1, 'tg-1', 1, '单车恋人')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (2, 'tg-2', 2, '无歌词')")
    conn.execute("INSERT INTO track_artist (track_id, artist_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(match_core, "MUSIC_DB", str(db))
    monkeypatch.setattr(match_core, "META_ROOT", str(meta))
    monkeypatch.setattr(match_core, "LYRIC_SQ_DIR", str(meta / "lyric-sqlite"))
    monkeypatch.setattr(match_core, "COVER_ROOT", str(meta / "cover"))
    monkeypatch.setattr(match_core, "_enabled_sources", lambda: ["musicdl"])
    return {"lib": lib, "meta": meta}


def test_delete_physical_removes_file_and_refs(env):
    lib = env["lib"]
    f1 = lib / "后弦 - 单车恋人.mp3"
    f1.write_bytes(b"ID3fake")
    lrc = lib / "后弦 - 单车恋人.lrc"
    lrc.write_text("[00:01.00]x")

    out = match_core.delete_tracks_physical(["tg-1"])
    assert out["okCount"] == 1 and out["failCount"] == 0
    assert not f1.exists() and not lrc.exists()

    conn = sqlite3.connect(match_core.MUSIC_DB)
    assert conn.execute("SELECT COUNT(*) FROM track WHERE guid='tg-1'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM track_artist WHERE track_id=1").fetchone()[0] == 0
    # 该 audio_file 已无 track 引用 → 整行删除（与去重同口径）
    assert conn.execute("SELECT COUNT(*) FROM audio_file WHERE id=1").fetchone()[0] == 0
    conn.close()
    # 其余曲目不受影响
    assert match_core.list_tracks(1, 50, "", "all")["total"] == 1


def test_delete_clears_lyric_shard_and_cover(env):
    match_core.write_lyric(1, "[00:01.00]x")
    match_core.write_cover(1, b"\xff\xd8fakejpeg")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    sg = conn.execute("SELECT stored_guid FROM lyric WHERE track_id=1").fetchone()[0]
    cg = conn.execute("SELECT cover_guid FROM track WHERE id=1").fetchone()[0]
    conn.close()
    shard = env["meta"] / "lyric-sqlite" / f"lyric-0{sg[0]}.db"
    cover = env["meta"] / "cover" / "track" / cg[:2] / cg
    assert shard.exists() and cover.exists()

    (env["lib"] / "后弦 - 单车恋人.mp3").write_bytes(b"x")
    out = match_core.delete_tracks_physical(["tg-1"])
    assert out["okCount"] == 1
    assert not cover.exists()
    oconn = sqlite3.connect(shard)
    n = oconn.execute("SELECT COUNT(*) FROM lyric_content WHERE guid=?", (sg,)).fetchone()[0]
    oconn.close()
    assert n == 0


def test_delete_refuses_shared_path(env):
    # 同一路径被另一首在库曲目引用（重复收录场景）：拒绝删除，防止把别人的文件干掉
    (env["lib"] / "后弦 - 单车恋人.mp3").write_bytes(b"x")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    shared_path = str(env["lib"] / "后弦 - 单车恋人.mp3")
    conn.execute("INSERT INTO audio_file (id, path, name) VALUES (3, ?, 'a.mp3')", (shared_path,))
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (3, 'tg-3', 3, '单车恋人副本')")
    conn.commit()
    conn.close()

    out = match_core.delete_tracks_physical(["tg-1"])
    assert out["okCount"] == 0 and out["failCount"] == 1
    assert "重复收录" in out["results"][0]["error"]
    assert (env["lib"] / "后弦 - 单车恋人.mp3").exists()


def test_delete_missing_file_still_cleans_db(env):
    # 文件早已不在（被手动删过）：照常清库记录
    out = match_core.delete_tracks_physical(["tg-1"])
    assert out["okCount"] == 1
    assert match_core.list_tracks(1, 50, "", "all")["total"] == 1


def test_delete_batch_isolates_failures(env):
    f1 = env["lib"] / "后弦 - 单车恋人.mp3"
    f1.write_bytes(b"x")
    out = match_core.delete_tracks_physical(["tg-1", "tg-404", ""])
    assert out["okCount"] == 1 and out["failCount"] == 1  # 空 guid 直接忽略
    assert out["results"][1]["ok"] is False
    assert not f1.exists()


# ---------------------------------------------------------------- 换源候选 --

CUR = {"guid": "g1", "title": "单车恋人", "artist": "后弦", "tier": qu.TIER_LOW,
       "durationMs": 249135}


def _item(**kw):
    base = {"id": "kuwo:1", "source": "kuwo", "title": "单车恋人", "artist": "后弦",
            "ext": "flac", "file_size": 48000000, "duration_s": 249.0, "album": "单车恋人",
            "cover_url": "http://x/1.jpg"}
    base.update(kw)
    return base


def test_shape_info_score_full():
    c = qu._shape_candidate(_item(), CUR)
    # 标题相等2 + 歌手吻合1 + 专辑1 + 封面1 + 时长吻合1 = 6
    assert c["infoScore"] == 6


def test_shape_info_score_local_artist_wrong():
    # 本地歌手信息是错的（换源修信息的典型场景）：候选歌手不吻合不加分，
    # 但标题/专辑/封面/时长照常计分，suspect 照常返回交人工判断
    c = qu._shape_candidate(_item(artist="另一个人"), CUR)
    assert c["kind"] == "suspect"
    assert c["infoScore"] == 5
    assert "album" in c and c["album"] == "单车恋人"


def test_shape_info_score_sparse():
    c = qu._shape_candidate(_item(album="", cover_url="", artist=""), CUR)
    # 标题2 + 歌手缺失不加分 + 无专辑 + 无封面 + 时长吻合1 = 3
    assert c["infoScore"] == 3


def test_shape_upgrade_mode_keeps_better_semantics():
    # 升级模式的 better 语义不受影响（回归保护）
    c = qu._shape_candidate(_item(), CUR)
    assert c["better"] is True
    cur = dict(CUR, tier=qu.TIER_HIGH)
    c2 = qu._shape_candidate(_item(ext="mp3", file_size=9000000), cur)
    assert c2["better"] is False


# ---------------------------------------------------------------- 内嵌信息 --

def test_meta_issues_computation():
    assert qu._meta_issues({"exists": True, "title": "x", "artist": "y",
                            "album": "z", "hasCover": True}) == []
    assert qu._meta_issues({"exists": True, "title": "", "artist": "y",
                            "album": "", "hasCover": False}) == ["标题", "专辑", "封面"]
    assert qu._meta_issues({"exists": False}) == ["文件缺失"]
    # 空白字符串也算缺失
    assert "歌手" in qu._meta_issues({"exists": True, "title": "x", "artist": "   ",
                                      "album": "z", "hasCover": True})


def test_list_tracks_meta_incomplete_filter(env, monkeypatch):
    """meta_incomplete：按文件实测标签过滤 + 内存分页 + 携带缺口清单。"""
    (env["lib"] / "后弦 - 单车恋人.mp3").write_bytes(b"x")
    (env["lib"] / "无歌词.mp3").write_bytes(b"y")
    # tg-1 标签齐全；tg-2 缺歌手+封面
    probes = {
        str(env["lib"] / "后弦 - 单车恋人.mp3"): {"exists": True, "title": "单车恋人",
                                                   "artist": "后弦", "album": "x",
                                                   "year": "2005", "trackNo": "1",
                                                   "hasCover": True},
        str(env["lib"] / "无歌词.mp3"): {"exists": True, "title": "无歌词",
                                          "artist": "", "album": "",
                                          "year": "", "trackNo": "",
                                          "hasCover": False},
    }
    monkeypatch.setattr(qu, "_probe_meta", lambda p: probes.get(p, {"exists": False}))

    out = qu.list_tracks(1, 50, "", "meta_incomplete")
    assert out["total"] == 1
    assert out["items"][0]["guid"] == "tg-2"
    assert out["items"][0]["metaIssues"] == ["歌手", "专辑", "封面"]

    # 全部标签齐全时列表为空
    monkeypatch.setattr(qu, "_probe_meta",
                        lambda p: {"exists": True, "title": "t", "artist": "a",
                                   "album": "b", "hasCover": True})
    assert qu.list_tracks(1, 50, "", "meta_incomplete")["total"] == 0

    # 文件缺失也算一条（引导用户处理脏记录）
    monkeypatch.setattr(qu, "_probe_meta", lambda p: {"exists": False})
    out = qu.list_tracks(1, 50, "", "meta_incomplete")
    assert out["total"] == 2 and all("文件缺失" in it["metaIssues"] for it in out["items"])
