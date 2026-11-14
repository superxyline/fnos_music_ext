"""曲库目录整理单测：规划（跨平台）与执行（Linux，挪文件+同步 audio_file.path）。

只测纯逻辑与临时模拟库；真机批量行为靠 NAS 小批量验证（同音质升级链路）。
"""
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import library_organize as lib_org  # noqa: E402
import match_core  # noqa: E402
from test_match import SCHEMA  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    db = tmp_path / "music.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE track ADD COLUMN metadata_mode INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE audio_file ADD COLUMN is_physical_file_deleted INTEGER DEFAULT 0")
    # 1: 有歌手有专辑 → 移动；2: 无歌手无专辑且无标签 → 跳过；3: 与 1 共享 audio_file（cue）
    rows = [
        (1, str(lib / "后弦 - 单车恋人.mp3")),
        (2, str(lib / "无词歌.mp3")),
    ]
    for af_id, path in rows:
        conn.execute("INSERT INTO audio_file (id, path, name) VALUES (?, ?, ?)",
                     (af_id, path, os.path.basename(path)))
    conn.execute("INSERT INTO album (id, name) VALUES (1, '宝贝')")
    conn.execute("INSERT INTO artist (id, name) VALUES (1, '后弦')")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title, album_id, is_cue) "
                 "VALUES (1, 'tg-1', 1, '单车恋人', 1, 0)")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title, is_cue) "
                 "VALUES (3, 'tg-3', 1, '单车恋人(CUE)', 1)")
    conn.execute("INSERT INTO track (id, guid, audio_file_id, title) VALUES (2, 'tg-2', 2, '无词歌')")
    conn.execute("INSERT INTO track_artist (track_id, artist_id) VALUES (1, 1)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(match_core, "MUSIC_DB", str(db))
    return {"lib": lib, "db": str(db)}


def _write(path, data=b"ID3fake"):
    with open(path, "wb") as handle:
        handle.write(data)


def test_plan_moves_by_artist_album(env):
    lib = env["lib"]
    _write(lib / "后弦 - 单车恋人.mp3")
    _write(lib / "无词歌.mp3")
    plan = lib_org.build_plan(str(lib), "artist_album")
    assert plan["total"] == 2  # audio_file 去重（cue 共享同一文件只算一次）
    assert plan["toMove"] == 2
    assert plan["missingPaths"] == []
    item = plan["preview"][0]
    assert item["artist"] == "后弦" and item["album"] == "宝贝"
    expected = os.path.join(str(lib), "后弦", "宝贝", "后弦 - 单车恋人.mp3")
    assert item["target"] == expected


def test_plan_no_meta_goes_to_unknown(env):
    """DB 无歌手无专辑且无标签 → 文件名解析歌手，专辑落未知。"""
    lib = env["lib"]
    _write(lib / "无词歌_某人.mp3")
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET path=? WHERE id=2", (str(lib / "无词歌_某人.mp3"),))
    conn.commit()
    conn.close()
    plan = lib_org.build_plan(str(lib), "artist_album")
    assert plan["toMove"] == 1
    item = plan["preview"][0]
    assert item["artist"] == "某人" and item["album"] == "未知专辑"
    assert item["target"] == os.path.join(str(lib), "某人", "未知专辑", "无词歌_某人.mp3")


def test_plan_no_meta_no_underscore_goes_unknown_artist(env):
    """文件名无下划线（不猜歌手）→ 未知歌手。"""
    lib = env["lib"]
    _write(lib / "无词歌.mp3")
    plan = lib_org.build_plan(str(lib), "artist_album")
    assert plan["preview"][0]["artist"] == "未知歌手"


def test_filename_artist_parse():
    pa = lib_org._filename_artist
    assert pa("/x/当你老了_赵照.mp3") == "赵照"
    assert pa("/x/后会无期_徐良 _ 汪苏泷.mp3") == "汪苏泷"
    assert pa("/x/后弦 - 单车恋人.mp3") == ""
    assert pa("/x/黄征 _ 羽·泉 - 奔跑.flac") == "羽·泉 - 奔跑"


def test_in_root_guard(tmp_path):
    lib = str(tmp_path / "lib")
    other = str(tmp_path / "other-lib")
    assert lib_org._in_root(os.path.join(lib, "a", "b.mp3"), lib) is True
    assert lib_org._in_root(os.path.join(lib, "a.mp3"), lib) is True
    assert lib_org._in_root(os.path.join(other, "a.mp3"), lib) is False
    assert lib_org._in_root("relative/a.mp3", lib) is False


def test_plan_unchanged_when_already_organized(env):
    target = env["lib"] / "后弦" / "宝贝"
    target.mkdir(parents=True)
    _write(target / "后弦 - 单车恋人.mp3")
    # DB 路径同步到已整理位置（模拟此前已整理过的状态）
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET path=? WHERE id=1",
                 (str(target / "后弦 - 单车恋人.mp3"),))
    conn.commit()
    conn.close()
    plan = lib_org.build_plan(str(env["lib"]), "artist_album")
    assert plan["unchanged"] == 1 and plan["toMove"] == 0
    assert plan["missing"] == 1  # 无词歌 DB 有记录但文件缺失


def test_plan_missing_file_counted(env):
    # DB 有记录但磁盘文件已删 → missing，且进缺失清单
    plan = lib_org.build_plan(str(env["lib"]), "artist_album")
    assert plan["missing"] == 2
    assert len(plan["missingPaths"]) == 2
    assert plan["toMove"] == 0


def test_plan_conflict_detected(env):
    lib = env["lib"]
    target = lib / "后弦" / "宝贝"
    target.mkdir(parents=True)
    _write(target / "同名挡路.mp3")
    _write(target / "后弦 - 单车恋人.mp3")  # 与将移入文件同名 → 冲突
    _write(lib / "后弦 - 单车恋人.mp3")
    plan = lib_org.build_plan(str(lib), "artist_album")
    assert plan["toMove"] == 1
    assert len(plan["conflicts"]) == 1
    assert plan["conflicts"][0]["path"] == str(lib / "后弦 - 单车恋人.mp3")


def test_plan_tag_fallback_when_db_missing_album(env, monkeypatch):
    """DB 缺专辑时回落文件标签；标签也缺 → 未知专辑目录。"""
    lib = env["lib"]
    _write(lib / "后弦 - 单车恋人.mp3")
    # 删掉 track 的 album_id，让 DB 元数据缺专辑
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE track SET album_id=NULL WHERE id=1")
    conn.commit()
    conn.close()

    monkeypatch.setattr(lib_org, "_tag_fallback", lambda path: ("后弦", "新专辑"), raising=True)
    plan = lib_org.build_plan(str(lib), "artist_album")
    assert plan["preview"][0]["album"] == "新专辑"

    monkeypatch.setattr(lib_org, "_tag_fallback", lambda path: ("后弦", ""), raising=True)
    plan = lib_org.build_plan(str(lib), "artist_album")
    item = plan["preview"][0]
    assert item["target"] == os.path.join(str(lib), "后弦", "未知专辑", "后弦 - 单车恋人.mp3")


@pytest.mark.skipif(sys.platform == "win32", reason="organize 引擎用 dir_fd，Linux 专用")
def test_execute_moves_files_and_updates_db(env, monkeypatch):
    monkeypatch.setattr(lib_org, "RECONCILE_WAIT_S", 0)  # 对账稳定循环不等真实间隔
    lib = env["lib"]
    audio = lib / "后弦 - 单车恋人.mp3"
    lrc = lib / "后弦 - 单车恋人.lrc"
    _write(audio, b"audio-bytes")
    lrc.write_text("[00:01.00]x")
    _write(lib / "无词歌.mp3")

    job = {"id": "t1", "root": str(lib), "layout": "artist_album", "dryRun": False,
           "status": "pending", "phase": "", "started_at": 0.0,
           "scanned": 0, "moved": 0, "skipped": 0, "failed": 0,
           "details": [], "errors": []}

    class Runtime:
        CONF = {"cache_dir": "", "music_db": env["db"]}
        import contextlib as _cl
        _LIBRARY_FILE_LOCK = _cl.nullcontext()

    asyncio.run(lib_org.run_library_organize(Runtime(), job))

    expected = lib / "后弦" / "宝贝" / "后弦 - 单车恋人.mp3"
    assert expected.is_file() and expected.read_bytes() == b"audio-bytes"
    assert (lib / "后弦" / "宝贝" / "后弦 - 单车恋人.lrc").is_file()
    assert not audio.exists() and not lrc.exists()
    # 无词歌：DB 无歌手无专辑且无标签 → 未知歌手/未知专辑
    unknown = lib / "未知歌手" / "未知专辑" / "无词歌.mp3"
    assert unknown.is_file()
    assert not (lib / "无词歌.mp3").exists()
    assert job["moved"] == 2 and job["failed"] == 0
    # DB 路径已同步、track/guid 行不动
    conn = sqlite3.connect(env["db"])
    paths = dict(conn.execute("SELECT id, path FROM audio_file").fetchall())
    guids = dict(conn.execute("SELECT guid, audio_file_id FROM track").fetchall())
    conn.close()
    assert paths[1] == str(expected)
    assert paths[2] == str(unknown)
    assert guids == {"tg-1": 1, "tg-3": 1, "tg-2": 2}


def test_reconcile_restores_watcher_flags(env):
    """watcher 批量软删的标志位被一轮对账恢复（跨平台：纯 sqlite+文件检查）。"""
    lib = env["lib"]
    _write(lib / "后弦 - 单车恋人.mp3")
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET path=? WHERE id=1", (str(lib / "后弦 - 单车恋人.mp3"),))
    conn.commit()
    conn.close()

    pre = lib_org._capture_flags([1])
    assert pre[1]["af_del"] == 0
    assert pre[1]["tracks"] == {1: (0, 0), 3: (0, 0)}  # 本轨 + cue 子轨

    # 模拟官方 watcher 批量软删
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET is_physical_file_deleted=1 WHERE id=1")
    conn.execute("UPDATE track SET is_audio_file_deleted=1 WHERE id=1")
    conn.commit()
    conn.close()

    af_fixed, track_fixed = lib_org._reconcile_flags_once([1], pre)
    assert (af_fixed, track_fixed) == (1, 1)
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT is_physical_file_deleted FROM audio_file WHERE id=1").fetchone()[0] == 0
    assert conn.execute("SELECT is_audio_file_deleted FROM track WHERE id=1").fetchone()[0] == 0
    # cue 子轨（tg-3）未被翻转也不受影响
    assert conn.execute("SELECT is_audio_file_deleted FROM track WHERE id=3").fetchone()[0] == 0
    conn.close()

    # 文件不在位时不恢复（防止复活已删文件）
    os.remove(lib / "后弦 - 单车恋人.mp3")
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET is_physical_file_deleted=1 WHERE id=1")
    conn.commit()
    conn.close()
    af_fixed, track_fixed = lib_org._reconcile_flags_once([1], pre)
    assert (af_fixed, track_fixed) == (0, 0)
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT is_physical_file_deleted FROM audio_file WHERE id=1").fetchone()[0] == 1
    conn.close()


@pytest.mark.skipif(sys.platform == "win32", reason="organize 引擎用 dir_fd，Linux 专用")
def test_execute_skips_outside_root(env, tmp_path):
    """🔴 根外文件绝不移动：DB 里指向整理根之外的文件必须原样保留。"""
    outside_dir = tmp_path / "other-lib"
    outside_dir.mkdir()
    outside_file = outside_dir / "后弦 - 单车恋人.mp3"
    _write(outside_file, b"outside-bytes")

    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE audio_file SET path=? WHERE id=1", (str(outside_file),))
    conn.commit()
    conn.close()

    job = {"id": "t2", "root": str(env["lib"]), "layout": "artist_album", "dryRun": False,
           "status": "pending", "phase": "", "started_at": 0.0,
           "scanned": 0, "moved": 0, "skipped": 0, "failed": 0,
           "details": [], "errors": []}

    class Runtime:
        CONF = {"cache_dir": "", "music_db": env["db"]}
        import contextlib as _cl
        _LIBRARY_FILE_LOCK = _cl.nullcontext()

    asyncio.run(lib_org.run_library_organize(Runtime(), job))

    assert outside_file.read_bytes() == b"outside-bytes"  # 根外文件纹丝不动
    assert job["moved"] == 0
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT path FROM audio_file WHERE id=1").fetchone()[0] == str(outside_file)
    conn.close()
