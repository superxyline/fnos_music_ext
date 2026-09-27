"""match_core 单测：临时模拟官方库（music.db + lyric-sqlite + cover），mock 音源。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import match_core  # noqa: E402

SCHEMA = """
CREATE TABLE audio_file (id INTEGER PRIMARY KEY, shared_library_id INTEGER, path TEXT,
    name TEXT, suffix TEXT, size INTEGER, duration_ms INTEGER);
CREATE TABLE album (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE artist (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE track_artist (id INTEGER PRIMARY KEY, track_id INTEGER, artist_id INTEGER, artist_order INTEGER);
CREATE TABLE track (id INTEGER PRIMARY KEY, guid TEXT, audio_file_id INTEGER, shared_library_id INTEGER,
    title TEXT, album_id INTEGER, cover_guid TEXT, year INTEGER, disc_no INTEGER, track_no INTEGER,
    duration_ms INTEGER, is_cue INTEGER, is_audio_file_deleted INTEGER DEFAULT 0,
    is_admin_deleted INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE lyric (id INTEGER PRIMARY KEY, guid TEXT, track_id INTEGER, source INTEGER,
    stored_guid TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE lyric_content (guid TEXT PRIMARY KEY, content BLOB, created_at DATETIME);
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    meta = tmp_path / "meta"
    (meta / "lyric-sqlite").mkdir(parents=True)
    (meta / "cover").mkdir(parents=True)
    db = tmp_path / "music.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO audio_file (id, path, name) VALUES (1, '/m/周杰伦 - 晴天.mp3', 'x.mp3')")
    conn.execute("INSERT INTO audio_file (id, path, name) VALUES (2, '/m/无歌词.mp3', 'y.mp3')")
    conn.execute("INSERT INTO artist (id, name) VALUES (1, '周杰伦')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (1, 'tg-1', 1, '晴天')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (2, 'tg-2', 2, '无歌词')")
    conn.execute("INSERT INTO track_artist (track_id, artist_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(match_core, "MUSIC_DB", str(db))
    monkeypatch.setattr(match_core, "META_ROOT", str(meta))
    monkeypatch.setattr(match_core, "LYRIC_SQ_DIR", str(meta / "lyric-sqlite"))
    monkeypatch.setattr(match_core, "COVER_ROOT", str(meta / "cover"))
    monkeypatch.setattr(match_core, "_enabled_sources", lambda: ["musicdl"])
    return meta


def test_list_tracks_and_filter(env):
    all_rows = match_core.list_tracks(1, 50, "", "all")
    assert all_rows["total"] == 2
    t1 = next(x for x in all_rows["items"] if x["guid"] == "tg-1")
    assert t1["title"] == "晴天" and t1["artist"] == "周杰伦"
    assert t1["hasLyric"] is False and t1["hasCover"] is False

    miss_lyric = match_core.list_tracks(1, 50, "", "missing_lyric")
    assert miss_lyric["total"] == 2

    kw = match_core.list_tracks(1, 50, "晴天", "all")
    assert kw["total"] == 1 and kw["items"][0]["guid"] == "tg-1"


def test_write_and_read_lyric(env):
    match_core.write_lyric(1, "[00:01.00]故事的小黄花\n[00:05.00]从出生那年就飘着")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    row = conn.execute("SELECT stored_guid FROM lyric WHERE track_id = 1").fetchone()
    conn.close()
    assert row and len(row[0]) == 32
    # 分片文件名 = lyric-0<首字符>.db
    sg = row[0]
    shard = env / "lyric-sqlite" / f"lyric-0{sg[0]}.db"
    assert shard.exists()
    text = match_core.read_lyric(1)
    assert "故事的小黄花" in text

    # 覆盖写：新 stored_guid、旧分片行删除
    old = sg
    match_core.write_lyric(1, "第二版歌词")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    new = conn.execute("SELECT stored_guid FROM lyric WHERE track_id = 1").fetchone()[0]
    conn.close()
    assert new != old
    old_shard = env / "lyric-sqlite" / f"lyric-0{old[0]}.db"
    oconn = sqlite3.connect(old_shard)
    assert oconn.execute("SELECT COUNT(*) FROM lyric_content WHERE guid=?", (old,)).fetchone()[0] == 0
    oconn.close()
    assert match_core.read_lyric(1) == "第二版歌词"

    # JSON 结构化歌词读取
    match_core.write_lyric(1, '{"t":0,"c":[{"tx":"行一"},{"tx":"行二"}]}')
    assert match_core.read_lyric(1) == "行一行二"


def test_write_cover(env):
    match_core.write_cover(1, b"\xff\xd8fakejpegdata")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    cg = conn.execute("SELECT cover_guid FROM track WHERE id=1").fetchone()[0]
    conn.close()
    path = env / "cover" / "track" / cg[:2] / cg
    assert path.exists() and path.read_bytes().startswith(b"\xff\xd8")

    # 覆盖写：旧封面文件删除
    old = cg
    match_core.write_cover(1, b"\x89PNGnewdata")
    conn = sqlite3.connect(match_core.MUSIC_DB)
    cg2 = conn.execute("SELECT cover_guid FROM track WHERE id=1").fetchone()[0]
    conn.close()
    assert cg2 != old
    assert not (env / "cover" / "track" / old[:2] / old).exists()
    assert (env / "cover" / "track" / cg2[:2] / cg2).exists()


def test_titles_match():
    assert match_core._titles_match("晴天", "晴天")
    assert match_core._titles_match("晴天", "晴天 (Live)")
    assert match_core._titles_match("Mojito", "mojito")
    assert not match_core._titles_match("晴天", "七里香")
    assert not match_core._titles_match("", "")


@pytest.mark.anyio
async def test_match_one_end_to_end(env, monkeypatch):
    async def fake_search(client, keyword):
        return [
            {"source": "musicdl", "id": "s1", "title": "晴天 (Live)", "artist": "周杰伦"},
            {"source": "musicdl", "id": "s2", "title": "七里香", "artist": "周杰伦"},
        ]

    async def fake_detail(client, cand):
        return {"lyric": "[00:01.00]歌词内容", "cover_url": "http://img/x.jpg"}

    async def fake_image(client, url):
        return b"IMGDATA" * 200

    monkeypatch.setattr(match_core, "search_candidates", fake_search)
    monkeypatch.setattr(match_core, "fetch_candidate_detail", fake_detail)
    monkeypatch.setattr(match_core, "_download_image", fake_image)

    import httpx

    async with httpx.AsyncClient() as client:
        res = await match_core.match_one(client, "tg-1", ["lyric", "cover"])
    assert res["ok"] and res["lyricOk"] and res["coverOk"], res
    assert res["matchedTitle"] == "晴天 (Live)"
    assert "歌词内容" in match_core.read_lyric(1)
    conn = sqlite3.connect(match_core.MUSIC_DB)
    assert conn.execute("SELECT cover_guid FROM track WHERE id=1").fetchone()[0]
    conn.close()

    # 标题不吻合 → 失败
    async def fake_search_miss(client, keyword):
        return [{"source": "musicdl", "id": "s9", "title": "完全无关", "artist": ""}]

    monkeypatch.setattr(match_core, "search_candidates", fake_search_miss)
    async with httpx.AsyncClient() as client:
        res2 = await match_core.match_one(client, "tg-2", ["lyric"])
    assert not res2["ok"] and "候选" in res2["error"], res2
