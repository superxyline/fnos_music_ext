#!/usr/bin/env python3
"""一次性运维脚本：把曲库里的 unknown 命名文件按收藏快照/文件标签改名。

用 proxy 的 venv 执行：
  .venv-proxy/bin/python scripts/repair_unknown_names.py [--apply]

只处理 guid->ref 记录指向 unknown* 且仍存在于曲库的音频（含同名 .lrc 随迁），
ref 更新、音频标签写入。默认 dry-run 打印计划，--apply 才落盘。
"""
import glob
import json
import os
import re
import sys

REPO = (
    sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else os.environ.get("FNMUSIC_HOME")
    or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
CACHE = os.environ.get("FNMUSIC_CACHE_DIR") or os.path.join(REPO, "cache")
FAV = os.environ.get("FNMUSIC_FAV_DIR") or os.path.join(REPO, "online_favorites")
EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".wma")
apply = "--apply" in sys.argv
print("REPO =", REPO)


def safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def is_unknown(stem: str) -> bool:
    base = os.path.basename(stem)
    return base == "unknown" or bool(re.fullmatch(r"unknown \(\d+\)", base))


def tags(path: str):
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None or getattr(audio, "tags", None) is None:
            return "", "", ""

        def first(key):
            v = audio.tags.get(key)
            if isinstance(v, (list, tuple)):
                v = v[0] if v else ""
            return str(v or "").strip()

        return first("title"), first("artist"), first("album")
    except Exception:
        return "", "", ""


def write_tags(path, title, artist, album):
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            audio.add_tags()
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        print("  [warn] 标签写入失败:", e)


def safe_name(s: str) -> str:
    t = re.sub(r"[/\\:\0]", "_", (s or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


# guid -> 快照
snaps = {}
for f in glob.glob(os.path.join(FAV, "*.json")):
    try:
        data = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    items = data.get("items") if isinstance(data, dict) else data
    for it in items or []:
        g = str(it.get("guid") or "")
        if g:
            snaps[g] = it.get("track") or {}

# 遍历 ref，找 unknown 目标
fixed = 0
for ref in sorted(glob.glob(os.path.join(CACHE, "*.ref"))):
    try:
        stem = open(ref, encoding="utf-8").read().strip()
    except Exception:
        continue
    if not is_unknown(stem):
        continue
    audio = next((stem + e for e in EXTS if os.path.exists(stem + e)), None)
    if not audio:
        continue
    guid = os.path.basename(ref)[:-4]
    snap = {}
    # ref 文件名是 safe guid，与快照 guid 正向匹配
    for g, t in snaps.items():
        if safe_guid(g) == guid:
            snap = t
            break
    title = str(snap.get("title") or "").strip()
    artist = str(snap.get("artist") or "").strip()
    album = str(snap.get("album") or "").strip()
    # 标签抢救：快照缺 title 或缺 artist/album 时都从文件自带 ID3 补
    if not title or not artist or not album:
        t2, a2, al2 = tags(audio)
        title = title or t2
        artist = artist or a2
        album = album or al2
    if not title:
        print("跳过(无歌名):", audio)
        continue
    ext = os.path.splitext(audio)[1]
    directory = os.path.dirname(stem)
    base = safe_name(artist) + " - " + safe_name(title) if artist and artist.lower() != title.lower() and artist != "unknown" else safe_name(title)
    dest = os.path.join(directory, base + ext)
    n = 2
    while os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(audio):
        dest = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    if apply:
        os.replace(audio, dest)
        old_lrc = stem + ".lrc"
        if os.path.exists(old_lrc):
            new_lrc = os.path.splitext(dest)[0] + ".lrc"
            if not os.path.exists(new_lrc):
                os.replace(old_lrc, new_lrc)
        write_tags(dest, title, artist, album)
        # ref 同步指向新 stem，否则 find_cache_file 失效会导致重复下载
        with open(ref, "w", encoding="utf-8") as fp:
            fp.write(os.path.splitext(dest)[0])
        print("已改名:", audio, "->", dest)
    else:
        print("计划:", audio, "->", dest)
    fixed += 1

print(f"{'完成' if apply else 'dry-run'}，共处理 {fixed} 个文件")
