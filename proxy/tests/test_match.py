"""match_core 单测：临时模拟官方库（music.db + lyric-sqlite + cover），mock 音源。"""
import asyncio
import os
import sqlite3
import sys
import time

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


@pytest.mark.anyio
async def test_start_task_prunes_expired(monkeypatch):
    """过期任务清理：曾因列表推导变量越作用域导致 start_task 抛 NameError（HTTP 500）。"""
    match_core._TASKS.clear()
    expired = time.monotonic() - match_core._TASK_TTL - 10
    match_core._TASKS["old"] = {"id": "old", "guids": [], "wants": [], "results": [],
                                "running": False, "createdAt": expired, "current": ""}
    match_core._TASKS["busy"] = {"id": "busy", "guids": [], "wants": [], "results": [],
                                 "running": True, "createdAt": expired, "current": ""}

    async def fake_match_one(client, guid, wants):
        return {"guid": guid, "ok": True, "lyricOk": True, "coverOk": False,
                "error": "", "matchedTitle": "t", "matchedArtist": ""}

    monkeypatch.setattr(match_core, "match_one", fake_match_one)
    try:
        tid = match_core.start_task(["g1"], ["lyric"])
        assert tid
        assert "old" not in match_core._TASKS          # 过期已清
        assert "busy" in match_core._TASKS             # 运行中不删
        for _ in range(60):
            t = match_core.get_task(tid)
            if t and not t["running"]:
                break
            await asyncio.sleep(0.05)
        t = match_core.get_task(tid)
        assert t["done"] == 1 and t["okCount"] == 1, t
    finally:
        match_core._TASKS.clear()


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------

DEDUP_SCHEMA = SCHEMA + """
CREATE TABLE favorite_track (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE playlist (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT);
CREATE TABLE playlist_track (id INTEGER PRIMARY KEY, user_id INTEGER, playlist_id INTEGER,
    track_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE play_history (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER,
    play_count INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_track_lyric_preference (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER,
    lyric_id INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE user_track_lyric_offset (id INTEGER PRIMARY KEY, user_id INTEGER, track_id INTEGER,
    lyric_id INTEGER, offset_ms INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE track_genre (id INTEGER PRIMARY KEY, track_id INTEGER, genre_id INTEGER);
"""


@pytest.fixture()
def dedup_env(tmp_path, monkeypatch):
    meta = tmp_path / "meta"
    (meta / "lyric-sqlite").mkdir(parents=True)
    db = tmp_path / "music.db"
    conn = sqlite3.connect(db)
    conn.executescript(DEDUP_SCHEMA)
    # 两副本同歌名：1=128k mp3，2=flac（好）
    for tid, (path, suffix) in enumerate([
        ("/m/song.mp3", "mp3"), ("/m/song2.flac", "flac"),
    ], start=1):
        conn.execute("INSERT INTO audio_file (id, path, name, suffix) VALUES (?, ?, ?, ?)",
                     (tid, path, "f" + suffix, suffix))
        conn.execute(
            "INSERT INTO track (id, guid, audio_file_id, title) VALUES (?, ?, ?, ?)",
            (tid, f"guid{tid}", tid, "同首歌名"),
        )
    # 引用：两副本都被 user1 收藏；old(1) 有播放历史+歌词
    conn.execute("INSERT INTO favorite_track (user_id, track_id) VALUES (1, 1)")
    conn.execute("INSERT INTO favorite_track (user_id, track_id) VALUES (1, 2)")
    conn.execute("INSERT INTO playlist (id, user_id, name) VALUES (1, 1, 'p')")
    conn.execute("INSERT INTO playlist_track (user_id, playlist_id, track_id) VALUES (1, 1, 1)")
    conn.execute("INSERT INTO play_history (user_id, track_id, play_count) VALUES (1, 1, 7)")
    conn.execute("INSERT INTO play_history (user_id, track_id, play_count) VALUES (1, 2, 3)")
    conn.execute("INSERT INTO lyric (id, guid, track_id, stored_guid) VALUES (1, 'lg1', 1, 'aabbccddeeff00112233445566778899')")
    conn.execute("INSERT INTO user_track_lyric_preference (user_id, track_id, lyric_id) VALUES (1, 1, 1)")
    conn.commit()
    conn.close()
    # 造两个音频文件
    (tmp_path).mkdir(exist_ok=True)
    for f in ("song.mp3", "song2.flac"):
        (tmp_path / f).write_bytes(b"X" * 2048)

    trash = tmp_path / "trash"
    monkeypatch.setattr(match_core, "MUSIC_DB", str(db))
    monkeypatch.setattr(match_core, "META_ROOT", str(meta))
    monkeypatch.setattr(match_core, "LYRIC_SQ_DIR", str(meta / "lyric-sqlite"))
    monkeypatch.setattr(match_core, "TRASH_DIR", str(trash))
    # audio_quality mock：song2.flac 更好
    def fake_q(path):
        loss = str(path).endswith(".flac")
        return {"format": "flac" if loss else "mp3", "lossless": loss,
                "bitrate": 1000000 if loss else 128000, "size": 2048}
    monkeypatch.setattr(match_core, "audio_quality", fake_q)
    # 修正路径指向 tmp（SQL 里的 /m/ 假路径）
    conn = sqlite3.connect(db)
    conn.execute("UPDATE audio_file SET path = ? WHERE id = 1", (str(tmp_path / "song.mp3"),))
    conn.execute("UPDATE audio_file SET path = ? WHERE id = 2", (str(tmp_path / "song2.flac"),))
    conn.commit()
    conn.close()
    return tmp_path


def test_scan_duplicates_orders_by_quality(dedup_env):
    rep = match_core.scan_duplicates()
    assert rep["totalGroups"] == 1 and rep["totalExtra"] == 1
    g = rep["groups"][0]
    assert g["keep"]["trackId"] == 2, "flac 应保留"
    assert [x["trackId"] for x in g["remove"]] == [1]
    assert g["remove"][0]["favCount"] == 1  # 报告带引用计数


def test_remove_duplicate_migrates_and_trashes(dedup_env):
    res = match_core.remove_duplicate(1, 2)  # 删 128k，留 flac
    assert res["moved"] and res["trashDir"] == str(dedup_env / "trash")
    # 文件移走
    assert not (dedup_env / "song.mp3").exists()
    assert (dedup_env / "trash" / "song.mp3").exists()
    # track/audio_file 删除，keep 仍在
    conn = sqlite3.connect(match_core.MUSIC_DB)
    assert conn.execute("SELECT COUNT(*) FROM track WHERE id=1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM track WHERE id=2").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audio_file WHERE id=1").fetchone()[0] == 0
    # 收藏迁移：user1 只剩 keep 的一条（冲突去重）
    favs = conn.execute("SELECT track_id FROM favorite_track WHERE user_id=1").fetchall()
    assert [f[0] for f in favs] == [2]
    # 播放历史合并：7+3=10
    ph = conn.execute("SELECT track_id, play_count FROM play_history WHERE user_id=1").fetchall()
    assert ph == [(2, 10)], ph
    # 歌词：keep 无歌词 → old 的行迁移过去
    assert conn.execute("SELECT track_id FROM lyric WHERE id=1").fetchone()[0] == 2
    # 歌词偏好（指向 old）已删
    assert conn.execute("SELECT COUNT(*) FROM user_track_lyric_preference WHERE track_id=1").fetchone()[0] == 0
    conn.close()


def test_remove_duplicate_rejects_different_title(dedup_env):
    conn = sqlite3.connect(match_core.MUSIC_DB)
    conn.execute("INSERT INTO audio_file (id, path, name, suffix) VALUES (9, '/m/x.mp3', 'x', 'mp3')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (9, 'g9', 9, '另一首歌')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        match_core.remove_duplicate(9, 2)


def test_remove_duplicate_keeps_shared_audio_file(dedup_env):
    """CUE 整轨：多个 track 共享 audio_file，删其一不能删 audio_file。"""
    conn = sqlite3.connect(match_core.MUSIC_DB)
    conn.execute("UPDATE track SET audio_file_id = 2 WHERE id = 1")  # 两 track 共享 audio 2
    conn.commit()
    conn.close()
    match_core.remove_duplicate(1, 2)
    conn = sqlite3.connect(match_core.MUSIC_DB)
    assert conn.execute("SELECT COUNT(*) FROM audio_file WHERE id=2").fetchone()[0] == 1
    conn.close()


def test_remove_duplicate_cross_device_fallback(dedup_env, monkeypatch):
    """回收站与曲库跨挂载点（os.replace EXDEV）时用 shutil.move 兜底。"""
    real_replace = os.replace

    def fake_replace(a, b):
        if str(a).endswith("song.mp3"):
            raise OSError(18, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(match_core.os, "replace", fake_replace)
    res = match_core.remove_duplicate(1, 2)
    assert res["moved"]
    assert not (dedup_env / "song.mp3").exists()
    assert (dedup_env / "trash" / "song.mp3").exists()
    conn = sqlite3.connect(match_core.MUSIC_DB)
    assert conn.execute("SELECT COUNT(*) FROM track WHERE id=1").fetchone()[0] == 0
    conn.close()
