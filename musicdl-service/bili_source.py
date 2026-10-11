"""B 站音源（自研）：搜索 / 标题清洗（正经歌名+歌手） / DASH 音轨解析。

musicdl 库自带的 BilibiliMusicClient 已失效（实测静默 0 条）；本模块用 B 站
官方 web API（无公开文档，实测 2026-10-11 可用：搜索无需 cookie/签名 0.43s、
playurl 0.18s、音轨直链免 Referer）。本模块不依赖 musicdl 库，可独立单测。

曲目标题清洗是本模块的核心：飞牛官方刮削按"歌名+歌手"匹配，视频标题
（"【4K修复】周杰伦 - 晴天"）必须清洗为正经歌名，否则落库即废。
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import httpx

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False

BILI_SOURCE = "BilibiliMusicClient"

_BILI_BASE = "https://api.bilibili.com"
_BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
MAX_DURATION_S = 20 * 60   # 超过 20 分钟的多为合集/长视频，不是单曲

_EM_RE = re.compile(r"</?em[^>]*>")
_BRACKETS_RE = re.compile(r"【[^】]*】|\[[^\]]*\]")
_NOISE_RE = re.compile(
    r"(?:官方(?:MV|视频|频道)?|完整版?|高清|4K|2K|2160[Pp]|1080[Pp]|720[Pp]|无损|Hi-?Res|"
    r"母带|音质|修复|重制|纯享|字幕|歌词版?|动态歌词|现场版?|翻唱|cover|Cover|COVER|"
    r"(?<![A-Za-z])MV(?![A-Za-z]))")
_VERSION_RE = re.compile(
    r"[（(]\s*(?:[^）)]{0,16}(?:版|Live|live|MV|mv|cover|Cover|4K|1080|2160|720|"
    r"高清|无损|修复|音质|Res)[^）)]{0,8})\s*[）)]")
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[）)]")
_SPACE_RE = re.compile(r"\s+")
_STRIP_EDGE_RE = re.compile(r"^[\s\-—–｜|/·,，。:：]+|[\s\-—–｜|/·,，。:：]+$")
_SEG_SPLIT_RE = re.compile(r"\s+(?:-|—|–|/)\s+|[｜|]")

_RESOLVE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bili")

logger = logging.getLogger("bili_source")


def _get_json(path: str, params: dict, timeout: float = 5.0) -> "dict | None":
    """B 站 web API GET；非 0 code / 网络失败返回 None（B 站限流表现为 412/空结果）。"""
    url = f"{_BILI_BASE}{path}"
    try:
        if HAS_CURL_CFFI:
            r = curl_requests.get(url, params=params, headers=_BILI_HEADERS,
                                  impersonate="chrome", timeout=timeout)
            status, payload = r.status_code, r.json()
        else:
            with httpx.Client(timeout=timeout) as cx:
                r = cx.get(url, params=params, headers=_BILI_HEADERS)
                status, payload = r.status_code, r.json()
    except Exception as exc:
        logger.warning("bili api %s failed: %s", path, type(exc).__name__)
        return None
    if status != 200:
        logger.warning("bili api %s http %s（多为风控限流，稍后重试可恢复）", path, status)
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.warning("bili api %s code=%r msg=%r", path,
                       payload.get("code") if isinstance(payload, dict) else "?",
                       str(payload.get("message"))[:60] if isinstance(payload, dict) else "?")
        return None
    return payload


def clean_text(text: str) -> str:
    """归一化：NFKC（全角/数学字母→ASCII）、剥高亮 em、去装饰括块/版本注/噪声词。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = _EM_RE.sub("", text)
    text = _BRACKETS_RE.sub(" ", text)
    text = _VERSION_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = _EMPTY_PAREN_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    return _STRIP_EDGE_RE.sub("", text).strip()


def _strip_known(song: str, artist: str) -> str:
    """歌手候选里去掉与歌名重复的部分（如"《晴天》- 晴天"→""）。"""
    artist = (artist or "").strip()
    if song and song.lower() in artist.lower():
        artist = artist.lower().replace(song.lower(), " ").strip()
        artist = _SPACE_RE.sub(" ", artist).strip(" -—–|")
    return artist


def parse_title(title: str, keyword: str) -> "tuple[str, str]":
    """视频标题 → (歌名, 歌手)。

    优先级：① 书名号《》内=歌名，书名号外清洗后为歌手；② 无书名号时按
    " - "/"｜"分段（单段再按无空格 "-" 拆），keyword 锚定（含关键词的段=歌名，
    段与关键词 token 完全相等得最高分，平局取更短段——歌名通常短于歌手名）；
    ③ 无锚定时默认"歌手 - 歌名"（B 站转载官方 MV 的主流标题形态）；
    ④ 单段含关键词且关键词不在开头：关键词起的前缀是歌手候选。
    """
    raw = unicodedata.normalize("NFKC", _EM_RE.sub("", title or ""))
    raw = _BRACKETS_RE.sub(" ", raw)

    kw_core = re.sub(r"\s+", "", keyword or "").lower()
    kw_tokens = [t.lower() for t in re.split(r"\s+", (keyword or "").strip()) if t]

    m = re.search(r"《([^》]+)》", raw)
    if m:
        song = clean_text(m.group(1))
        outside = clean_text(raw[: m.start()] + " " + raw[m.end():])
        return song, _strip_known(song, outside)

    segs = [s.strip() for s in _SEG_SPLIT_RE.split(clean_text(raw)) if s.strip()]
    if len(segs) == 1 and "-" in segs[0]:
        segs = [s.strip() for s in segs[0].split("-") if s.strip()]

    if len(segs) >= 2:
        def score(seg: str) -> float:
            low = seg.lower().strip()
            if kw_tokens and any(t == low for t in kw_tokens):
                return 2.5  # 段与关键词 token 完全相等：几乎必是歌名
            if kw_core and kw_core in low:
                return 2.0
            if kw_tokens:
                hit = sum(1 for t in kw_tokens if t in low)
                if hit:
                    return 1.0 + hit / (2 * len(kw_tokens))
            return 0.0
        scores = [score(s) for s in segs]
        best = max(range(len(segs)), key=lambda i: (scores[i], -len(segs[i])))
        if scores[best] > 0:
            song = segs[best]
            artist = " ".join(segs[i] for i in range(len(segs)) if i != best)
        else:
            song, artist = segs[1], segs[0]
        return clean_text(song), clean_text(_strip_known(song, artist))

    single = clean_text(raw)
    if kw_tokens:
        for t in kw_tokens:
            pos = single.lower().find(t)
            if pos > 0:
                # 关键词不在开头：前缀是歌手候选（"周杰伦 Jay Chou 说好不哭 with 五月天"）
                return clean_text(single[pos:]), clean_text(single[:pos])
    return single, ""


def dur_to_s(raw: str) -> int:
    """'4:30' / '1:02:03' → 秒；异常返回 0（不过滤）。"""
    parts = [p for p in str(raw or "").strip().split(":") if p.strip().isdigit()]
    if not parts:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds


def search_videos(keyword: str, limit: int, timeout: float = 5.0) -> "list[dict]":
    """B 站视频搜索（过滤超长合集），[{bvid,title,author,duration_s,cover}]。

    B 站对高频搜索有间歇性限流（HTTP 200 空结果或 412）：空结果退避 1s 重试
    一次可吸收大部分抖动窗口（实测 2026-10-11）。
    """
    for attempt in (1, 2):
        data = _get_json("/x/web-interface/search/type", {
            "search_type": "video", "keyword": keyword,
            "page": 1, "page_size": min(30, limit * 3),
        }, timeout=timeout)
        if data is None:
            if attempt == 1:
                time.sleep(1.0)
                continue
            return []
        rows = (data.get("data") or {}).get("result") or []
        if not rows and attempt == 1:
            # code=0 但结果空：同为限流形态，退避重试一次
            time.sleep(1.0)
            continue
        out = []
        for it in rows:
            if not isinstance(it, dict) or not it.get("bvid"):
                continue
            dur = dur_to_s(it.get("duration"))
            if dur and dur > MAX_DURATION_S:
                continue
            out.append({
                "bvid": it.get("bvid"),
                "title": it.get("title") or "",
                "author": it.get("author") or "",
                "duration_s": dur,
                "cover": it.get("pic") or "",
            })
            if len(out) >= limit:
                break
        return out
    return []


def resolve_audio(bvid: str, timeout: float = 5.0) -> "tuple[str, int] | None":
    """bvid → (音轨直链, 时长秒)。view 拿 cid，playurl(DASH) 选最高码率音轨。"""
    view = _get_json("/x/web-interface/view", {"bvid": bvid}, timeout=timeout)
    if not view:
        return None
    vdata = view.get("data") or {}
    cid = vdata.get("cid")
    duration = int(vdata.get("duration") or 0)
    if not cid:
        return None
    play = _get_json("/x/player/playurl",
                     {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0}, timeout=timeout)
    if not play:
        return None
    audios = ((play.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audios:
        return None
    best = max(audios, key=lambda a: a.get("bandwidth") or 0)
    url = best.get("baseUrl") or best.get("base_url")
    if not url or not url.startswith(("http://", "https://")):
        return None
    return url, duration


class BiliSong:
    """duck-typing musicdl SongInfo：字段与 musicdl-service 的 _normalize 对齐。"""

    def __init__(self, identifier: str, song_name: str, singers: str, duration_s: int,
                 cover_url: str, download_url: str):
        self.source = BILI_SOURCE
        self.identifier = identifier
        self.song_name = song_name
        self.singers = singers
        self.album = ""
        self.duration_s = duration_s
        self.ext = "m4a"
        self.file_size_bytes = 0
        self.cover_url = cover_url
        self.download_url = download_url
        self.albummid = ""
        self.lyric = ""
        self.default_download_headers = dict(_BILI_HEADERS)


def search_songs(keyword: str, limit: int, timeout: float = 5.0) -> "list[BiliSong]":
    """B 站搜索：清洗标题出正经歌名/歌手，前若干条并发解析音轨直链。"""
    videos = search_videos(keyword, max(limit * 2, 6), timeout=timeout)
    if not videos:
        return []
    parsed = []
    for v in videos:
        title, artist = parse_title(v["title"], keyword)
        if not title:
            continue
        if not artist and v.get("author"):
            artist = clean_text(v["author"])
        parsed.append((v, title, artist))

    def resolve(entry):
        v, title, artist = entry
        resolved = resolve_audio(v["bvid"], timeout=timeout)
        if not resolved:
            return None
        url, dur = resolved
        return BiliSong(v["bvid"], title, artist, dur or v["duration_s"],
                        v["cover"], url)

    futures = [_RESOLVE_POOL.submit(resolve, e) for e in parsed[: max(limit, 4)]]
    songs = []
    for fut in futures:
        try:
            song = fut.result(timeout=timeout * 3)
        except Exception:
            song = None
        if song and song.download_url:
            songs.append(song)
        if len(songs) >= limit:
            break
    return songs
