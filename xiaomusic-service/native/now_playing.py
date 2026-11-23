"""当前播放曲目解析（纯标准库，不依赖 xiaomusic 上游，便于单测）。

为什么单独成模块（2026-11-23 用户反馈）：
上游 xiaomusic 在随机列表播完一轮时会在**后台预缓存下一首**，预缓存同样
请求桥接 `/play` 取流，把 `last_played_guid` 覆盖成下一首——点《就是爱你》
后 30 秒说「收藏这首歌」，收藏到的是预缓存的《分手季节》。

真正的"当前播放"是设备上报的 `cur_music`：拿它反查 `music_library.all_music`
里的桥接 URL（`/play?guid=...`）才是权威 guid。反查不到（未开播、或播的是
下载到本地的 mp3 文件）才退回 `last_played_*`。
"""
import re
import urllib.parse

_GUID_RE = re.compile(r"[?&]guid=([^&]+)")
# 音源标记：「[kuwo]就是爱你」→「就是爱你」，避免 TTS 念出英文/影响显示
_SOURCE_TAG_RE = re.compile(r"\[[A-Za-z0-9_\-]{1,12}\]")


def resolve_current_guid(devices, all_music, last_guid="", last_title="", did=""):
    """返回 (guid, 歌名)：设备当前曲目优先，last_played 兜底。"""
    dev = (devices or {}).get(did) if did else None
    info = getattr(dev, "device", None) if dev is not None else None
    name = (getattr(info, "cur_music", "") or "").strip() if info is not None else ""
    if name:
        url = (all_music or {}).get(name, "") or ""
        m = _GUID_RE.search(url)
        if m:
            return urllib.parse.unquote(m.group(1)), name
    return str(last_guid or ""), str(last_title or "")


def clean_display_name(name: str) -> str:
    """展示/TTS 用的歌名：去掉音源标记与多余空白。"""
    cleaned = _SOURCE_TAG_RE.sub("", name or "").strip()
    return cleaned or (name or "").strip()
