"""官方行元数据补写单测（文件名整段入库 → 刮削形态）。"""
import os
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy.app as proxy_app
from proxy.app import _repair_filename_track_metadata


def _make_db(tmp_path):
    db = tmp_path / "music.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        create table track (id integer primary key, guid text unique, title text,
            is_audio_file_deleted integer default 0, updated_at text);
        create table artist (id integer primary key, guid text, name text, metadata_mode integer);
        create table track_artist (id integer primary key, track_id integer, artist_id integer, artist_order integer default 0);
    """)
    conn.execute("insert into track (guid, title) values (?, ?)",
                 ("g1", "谢霆锋 - 因为爱所以爱"))
    conn.execute("insert into track (guid, title) values (?, ?)", ("g2", "晴天"))
    conn.commit()
    conn.close()
    return str(db)


def test_repair_filename_row(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setitem(proxy_app.CONF, "music_db", db)
    _repair_filename_track_metadata("g1", "因为爱所以爱", "谢霆锋")
    conn = sqlite3.connect(db)
    title = conn.execute("select title from track where guid='g1'").fetchone()[0]
    assert title == "因为爱所以爱"
    artists = conn.execute(
        "select a.name, ta.artist_order from artist a join track_artist ta on ta.artist_id=a.id"
    ).fetchall()
    assert artists == [("谢霆锋", 0)]
    conn.close()


def test_repair_multi_artists_and_idempotent(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setitem(proxy_app.CONF, "music_db", db)
    _repair_filename_track_metadata("g1", "奔跑", "黄征 / 羽·泉")
    _repair_filename_track_metadata("g1", "奔跑", "黄征 / 羽·泉")  # 幂等：不重复插
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "select a.name from artist a join track_artist ta on ta.artist_id=a.id order by ta.artist_order"
    ).fetchall()
    assert [r[0] for r in rows] == ["黄征", "羽·泉"]
    conn.close()


def test_repair_skips_clean_row(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setitem(proxy_app.CONF, "music_db", db)
    _repair_filename_track_metadata("g2", "晴天", "周杰伦")
    conn = sqlite3.connect(db)
    title = conn.execute("select title from track where guid='g2'").fetchone()[0]
    # 只有 artist 缺失才补；title 保持原样（无分隔符形态不动）
    assert title == "晴天"
    conn.close()
