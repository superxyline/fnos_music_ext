"""小米曲库 audio_id 解析：让带屏小爱（X4B 等）播放界面显示正确的歌曲信息。

背景（2026-10-20 对 xiaomusic 0.6.1 + miservice-fork 2.9.3 源码级定位）：
带屏设备播放走 mina ubus `player_play_music`，指令里的 audio_id 决定屏幕显示的
歌名/歌手/封面/歌词（播放内容则是桥接下发的 URL，两者本无绑定）。xiaomusic 上游
`XiaoMusicDevice._get_audio_id` 用歌名去小米 /music/search 搜 audioID，但：

1. 它按「歌名-歌手」格式拆名（`target_song = parts[0]`），而本桥接歌单条目名
   是「歌手 - 歌名」格式（artist 在前，见 app.py build_playlist）——拆出来的
   "歌名"其实是歌手名，与曲库 song.name 永远不相等，只能掉到 songList[0]
   兜底：蒙对→显示正确，蒙错→屏幕显示另一首歌（用户反馈"偶尔显示错误"的
   机制性根因）；
2. 标题带 feat./Live/Remix 等后缀、全半角差异时同样不命中；
3. 每次播放都现搜，命中不了就反复打接口。

本模块被 app.py 的 patch_audio_id_resolver 包装进上游 _get_audio_id：
- 歌单同步时批量预解析，落盘 bridge/audio_id_cache.json（重启不丢；负缓存
  防反复打接口；播歌时按需解析作兜底）；
- 「歌手 - 歌名」/「歌名 - 歌手」双格式解析 + 标题规范化（去修饰后缀）；
- 三级匹配：严格（歌名相等 + 歌手互含）→ 宽松（歌名相等）→ 近似（歌名互含）。

注意：audio_id 只影响屏幕显示（与歌词时间轴），不决定播放内容——播放的
始终是桥接 /play 下发的 NAS 音频流。
"""

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path

log = logging.getLogger("bridge")

# 与上游 _get_audio_id 的默认值保持一致（解析不出时的占位）
DEFAULT_AUDIO_ID = "1582971365183456177"
CACHE_VERSION = 1
SEARCH_PATH = "/music/search"


# ---------------- 文本规范化 ----------------

# 繁→简转换（飞牛刮削元数据偶有繁体，如"身體/毀/時間/賞"，小米曲库是简体，
# 不归一则严格匹配必失败——2026-10-20 预解析实测 275 首中 5 首因此未命中）。
# opencc-python-reimplemented 在容器镜像必装（requirements-core.txt）；
# 本地无该包时降级跳过（只影响繁体命中率，不影响功能）。
_CC = None


def _t2s(s: str) -> str:
    global _CC
    if _CC is None:
        try:
            from opencc import OpenCC
            _CC = OpenCC("t2s")
        except Exception:
            _CC = False   # False = 环境无 opencc，跳过转换
    if _CC is False:
        return s
    try:
        return _CC.convert(s)
    except Exception:
        return s


def normalize_text(s: str) -> str:
    """繁→简 + NFKC（全角→半角）+ 小写 + 压缩空白，用于两侧对称比较。"""
    if not s:
        return ""
    s = _t2s(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("　", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


# 括号修饰后缀：feat./ft./with 客串、Live/Remix/Demo 等版本标记
_TAG_RES = [
    re.compile(
        r"[(\[（【]\s*(feat\.?|ft\.?|with|prod\.?)[^)\]}）】]*[)\]）】]", re.I),
    re.compile(
        r"[(\[（【]\s*(live|remix|demo|acoustic|instrumental|remaster\w*|"
        r"explicit|clean|radio edit|bonus track|deluxe|mix|version)[^)\]}）】]*"
        r"[)\]）】]", re.I),
]
# 无括号连字符后缀："Song - Remastered 2020" / "Song - Live"
_DASH_TAG_RE = re.compile(
    r"\s*[-–—]\s*(remaster\w*|live|remix|demo|acoustic|instrumental|mono|stereo)"
    r"(\s+\d{4})?\s*$", re.I)


def strip_tags(s: str) -> str:
    """去掉标题里的客串/版本修饰（括号包裹或连字符后缀），返回主体。"""
    if not s:
        return ""
    out = s
    for pat in _TAG_RES:
        out = pat.sub("", out)
    out = _DASH_TAG_RE.sub("", out)
    out = re.sub(r"\s+", " ", out).strip(" -–—·")
    return out or s


# ---------------- 拆名与候选查询 ----------------

def gen_query_candidates(name: str) -> list[tuple[str, str, str]]:
    """从歌单条目名生成候选查询（title, artist, query），按优先级排序。

    条目名由桥接 build_playlist 生成，格式为「歌手 - 歌名」（artist 在前）；
    点歌注入/本地文件名则常见「歌名 - 歌手」。两种格式都解析，拆首处连字符
    （容忍 - – —，两侧可有空白）。返回的 query 去重。
    """
    name = (name or "").strip()
    if not name:
        return []
    cands: list[tuple[str, str]] = []
    parts = re.split(r"\s*[-–—]\s*", name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        a, b = parts[0].strip(), parts[1].strip()
        cands.append((b, a))          # 桥接格式：artist - title
        cands.append((a, b))          # 文件名常见格式：title - artist
    cands.append((name, ""))          # 整体作为最后兜底
    seen, out = set(), []
    for title, artist in cands:
        # query 也转简体：小米 /music/search 对简体命中率更高
        query = _t2s(f"{artist} {title}".strip())
        key = (normalize_text(title), normalize_text(artist))
        if key not in seen and query:
            seen.add(key)
            out.append((title, artist, query))
    return out


def _artist_hit(artist: str, song_artist: str) -> bool:
    """歌手匹配：归一化互含即算命中。我们的歌单只取第一歌手，曲库侧可能是
    "周杰伦;蔡依林" 形式，故对多歌手分隔符也做分词包含。"""
    a = normalize_text(artist)
    s = normalize_text(song_artist)
    if not a or not s:
        return True
    if a in s or s in a:
        return True
    if any(sep in a for sep in ["/", ";", "；", ",", "，", "&", "、", "|"]):
        parts = [p.strip() for p in re.split(r"[/;；,，&、|]", a) if p.strip()]
        return any(p in s for p in parts)
    return False


def match_audio_id(song_list, title: str, artist: str = ""):
    """三级匹配，返回 audioID 或 None。

    1. 严格：歌名 strip 后相等且歌手互含；
    2. 宽松：歌名相等（歌手对不上也认——屏幕至少歌名是对的，歌声是 URL 的）；
    3. 近似：歌名互含（"稻香 (合唱版)" / "稻香 live"），取第一个。
    """
    norm_title = normalize_text(strip_tags(title))
    if not norm_title or not song_list:
        return None
    songs = []
    for song in song_list:
        s_name = normalize_text(strip_tags(song.get("name") or ""))
        s_artist = normalize_text(strip_tags((song.get("artist") or {}).get("name") or ""))
        if not s_name:
            continue
        songs.append((s_name, s_artist, song))
    # 1. 严格
    for s_name, s_artist, song in songs:
        if s_name == norm_title and _artist_hit(artist, s_artist):
            return song.get("audioID")
    # 2. 宽松
    for s_name, _, song in songs:
        if s_name == norm_title:
            return song.get("audioID")
    # 3. 近似（防单字误配：标题至少 2 个字符）
    if len(norm_title) >= 2:
        for s_name, _, song in songs:
            if norm_title in s_name or s_name in norm_title:
                return song.get("audioID")
    return None


# ---------------- 搜索 ----------------

async def search_audio_id(mina_service, name: str, *, max_queries: int = 3,
                          interval: float = 0.15) -> str:
    """多候选依次调小米 /music/search，命中返回 audioID，全部未命中返回 ""。

    接口异常（登录失效/风控/网络）直接向上抛：调用方据此中止本轮预解析，
    不计负缓存（负缓存只表达"查过，曲库没有"）。
    """
    for i, (_title, _artist, query) in enumerate(gen_query_candidates(name)[:max_queries]):
        if i:
            await asyncio.sleep(interval)
        resp = await mina_service.mina_request(SEARCH_PATH, {
            "query": query,
            "queryType": 1,
            "offset": 0,
            "count": 6,
            "timestamp": int(time.time_ns() / 1000),
        })
        song_list = (resp or {}).get("data", {}).get("songList") or []
        audio_id = match_audio_id(song_list, _title, _artist)
        if audio_id:
            return str(audio_id)
    return ""


# ---------------- 缓存 ----------------

class AudioIdCache:
    """audio_id 缓存：内存 dict + JSON 落盘（原子替换）。

    key = 原始条目名；value = audioID，"" 表示负缓存（查过，曲库没有）。
    get 返回 None 表示未解析（需要现场解析/预解析）。
    """

    def __init__(self, path):
        self.path = Path(path)
        self._entries: dict[str, str] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                self._entries = {
                    str(k): str(v)
                    for k, v in (data.get("entries") or {}).items()
                    if str(k).strip()
                }
        except Exception:
            self._entries = {}

    def get(self, name):
        return self._entries.get((name or "").strip())

    def set(self, name, audio_id) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        val = str(audio_id or "")
        if self._entries.get(name) == val:
            return False
        self._entries[name] = val
        return True

    def prune(self, keep_names) -> int:
        """清掉不在当前歌单里的条目（防缓存无限增长），返回清理条数。"""
        keep = {(n or "").strip() for n in keep_names}
        before = len(self._entries)
        self._entries = {k: v for k, v in self._entries.items() if k in keep}
        return before - len(self._entries)

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"version": CACHE_VERSION, "entries": self._entries},
                           ensure_ascii=False),
                encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("[audio_id] 缓存落盘失败: %s", e)

    def stats(self) -> dict:
        resolved = sum(1 for v in self._entries.values() if v)
        return {"resolved": resolved, "negative": len(self._entries) - resolved}


# ---------------- 批量预解析 ----------------

async def resolve_audio_ids(mina_service, names, cache: AudioIdCache, *,
                            interval: float = 0.15,
                            deadline_sec: float = 240) -> dict:
    """批量预解析歌单条目名 → audio_id 并落盘。

    - 只处理未缓存的条目（增量；新增收藏的歌自动进入队列）；
    - 接口异常（登录失效等）立即中止本轮，且**不写负缓存**——登录恢复后
      下次同步会重新解析，避免把"没能查"永久缓存成"曲库没有"；
    - deadline 兜底防单轮跑太久。
    """
    todo = []
    for n in dict.fromkeys(names):
        if (n or "").strip() and cache.get(n) is None:
            todo.append(n.strip())
    stats = {"pending": len(todo), "hit": 0, "miss": 0, "aborted": 0}
    deadline = time.monotonic() + deadline_sec
    for name in todo:
        if time.monotonic() > deadline:
            stats["aborted"] = len(todo) - stats["hit"] - stats["miss"]
            log.warning("[audio_id] 预解析超时中止，剩余 %d 首下轮继续", stats["aborted"])
            break
        try:
            audio_id = await search_audio_id(mina_service, name, interval=interval)
        except Exception as e:
            stats["aborted"] = len(todo) - stats["hit"] - stats["miss"]
            log.warning("[audio_id] 预解析接口异常，本轮中止（不写负缓存）: %s", e)
            break
        cache.set(name, audio_id)
        if audio_id:
            stats["hit"] += 1
        else:
            stats["miss"] += 1
    cache.save()
    return stats
