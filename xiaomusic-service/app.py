"""fnmusic-xiaomusic 桥接（fnmusic-ext 内置版）

由 fnmusic-xiaomusic 项目（v0.2.1，superxyline）移植，作为 fnmusic-ext
fnmusic-sources 容器内的第 5 个 supervisor 进程运行（默认 8005，宿主映射 8775）：

  - 集成 xiaomusic（pip 安装）：小米账号登录、小爱音箱接管、口令轮询、播放控制
  - 桥接飞牛音乐：收藏同步、歌单生成、带认证音频流代理
  - 配置入口：fnmusic-ext 管理台「小爱音箱」页（WebUI 代理本服务的向导 API）；
    本服务自带的 / 页面保留作旁路调试入口

与独立 FPK 版的区别：
  - 数据目录 /data/xiaomusic/{conf,music,bridge}（sources-data 卷内独立子目录）
  - 双端口分离：内部监听 BRIDGE_PORT（8005，WebUI/xiaomusic 自身内环调用）；
    推送给音箱的外部地址 = http://{nas_ip}:{PUBLIC_PORT}（宿主映射 8775）
  - 飞牛音乐地址默认取容器默认网关（宿主 docker0），音箱访问地址由用户在
    管理台配置页确认（NAS 局域网 IP）
"""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import random
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from fnmusic_client import FnMusicClient, sign_authx

# ---------------- 配置 ----------------
# 数据目录独立子目录：避免与 musicbox/lxmusic 等服务的数据混居（同卷 /data）
DATA_DIR = Path(os.getenv("XIAOMUSIC_DATA_DIR", "/data/xiaomusic"))
CONF_DIR = DATA_DIR / "conf"          # xiaomusic 配置（setting.json / auth.json / .mi.token）
MUSIC_DIR = DATA_DIR / "music"        # xiaomusic 音乐目录（本方案只用网络歌单，保持为空）
BRIDGE_DIR = DATA_DIR / "bridge"      # 桥接数据
WIZARD_STATE_FILE = BRIDGE_DIR / "wizard_state.json"
BRIDGE_CONFIG_FILE = BRIDGE_DIR / "config.json"

# 内部监听端口（容器内）：WebUI 与 xiaomusic 自身的内环调用都走它
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8005"))
# 对外端口（宿主映射）：xiaomusic 推给音箱的 URL 用 http://{nas_ip}:{PUBLIC_PORT}
PUBLIC_PORT = int(os.getenv("XIAOMUSIC_PUBLIC_PORT", "8775"))
SELF_URL = f"http://127.0.0.1:{BRIDGE_PORT}"


def _default_gateway() -> str:
    """容器默认网关 IP（= 宿主 docker0）。飞牛音乐跑在宿主上，HTTP 走网关可达。"""
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            for line in fh.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "00000000":
                    gw = parts[2]
                    return ".".join(str(int(gw[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except Exception:
        pass
    return ""


GATEWAY_IP = _default_gateway()
# nas_ip 是「音箱访问本机的地址」，必须是 NAS 局域网 IP（由管理台配置页确认）；
# 未配置时先用网关兜底（至少保证桥接→飞牛音乐的方向可用）。
NAS_IP = os.getenv("NAS_IP") or GATEWAY_IP or "127.0.0.1"

log = logging.getLogger("bridge")

DEFAULT_CONFIG = {
    "nas_ip": NAS_IP,
    # 注意：容器内 127.0.0.1 是容器自身，飞牛音乐在 NAS 宿主网络，
    # 默认地址用容器网关（宿主 docker0）；音箱访问地址需用户在管理台确认为 NAS 局域网 IP
    "fnmusic_base_url": f"http://{NAS_IP}:5666",
    "fnmusic_username": "",
    "fnmusic_password": "",
    "playlist_name": "本地收藏",
    "playlist_keyword": "播放本地收藏歌曲",
    "sync_interval": 300,
    "transcode_mode": "auto",
    "wizard_done": False,
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


CFG = load_json(BRIDGE_CONFIG_FILE, dict(DEFAULT_CONFIG))

# ---------------- xiaomusic 集成 ----------------
# 注意：XiaoMusic 构造内部会创建 aiohttp.ClientSession，必须在有事件循环的
# 异步上下文中执行，因此延迟到 FastAPI lifespan 里创建（uvicorn 保证
# lifespan 先于任何请求完成，动态 mount 安全）。

XM = None   # XiaoMusic 实例
XM_APP = None  # xiaomusic 的 FastAPI 子应用


def create_xiaomusic():
    """创建 XiaoMusic 实例并初始化其 FastAPI 子应用（在 lifespan 中调用）"""
    global XM, XM_APP
    for d in (CONF_DIR, MUSIC_DIR, MUSIC_DIR / "tmp", MUSIC_DIR / "download",
              MUSIC_DIR / "cache", BRIDGE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    from xiaomusic.config import Config
    from xiaomusic.xiaomusic import XiaoMusic

    cfg = Config(
        conf_path=str(CONF_DIR),
        music_path=str(MUSIC_DIR),
        temp_path=str(MUSIC_DIR / "tmp"),
        download_path=str(MUSIC_DIR / "download"),
        cache_dir=str(MUSIC_DIR / "cache"),
        cache_max_size_mb=200,
        hostname=f"http://{CFG.get('nas_ip', NAS_IP)}",
        port=8090,                      # 内嵌 mount，不单独监听
        public_port=PUBLIC_PORT,        # 推送给音箱的对外端口（宿主映射 8775）
        ffmpeg_location="/usr/bin",     # fnmusic-ext 镜像 apt 安装的 ffmpeg
        account="",
        password="",
        enable_analytics=False,
        disable_download=True,          # 不需要下载功能
        enable_file_watch=False,        # 音乐目录为空，无需监控
        web_music_proxy=True,           # 网络歌单走 xiaomusic 代理
        delay_sec=0,
        continue_play=True,
        get_duration_type="mutagen",      # 用 mutagen 算时长，免装 ffprobe
        # 指令拉取通道：用「小爱对话记录」API（LATEST_ASK_API，实测可用）。
        # 注意：不要开 get_ask_by_mina——实测 mina 的 ubus nlp_result_get 在
        # 本机固件上走 ROM 代理模式，只返回 {code:0, data:{code:0}} 没有 info
        # 字段，conversation.py 每次都抛 KeyError('info')，口令永远拿不到。
        get_ask_by_mina=False,
        # 必须显式开启指令轮询：XIAOMUSIC_ENABLE_PULL_ASK 默认 false，
        # poll_latest_ask 主循环第一行 `if not enable_pull_ask: sleep(5); continue`
        # 直接空转——口令永远到不了 xiaomusic（音箱自带的音乐技能接走全部语音）。
        enable_pull_ask=True,
    )
    XM = XiaoMusic(cfg)

    # 强制修正关键配置：XiaoMusic 构造时会用 setting.json（可能是 v1 迁移来的旧数据）
    # 全覆盖我们传入的值——conf_path 被改成相对路径会导致配置写到容器内而非 /data，
    # hostname/public_port/music_list_url 停留在 v1 值会导致音箱拉到错误地址。
    XM.config.update_config({
        "conf_path": str(CONF_DIR),
        "music_path": str(MUSIC_DIR),
        "temp_path": str(MUSIC_DIR / "tmp"),
        "download_path": str(MUSIC_DIR / "download"),
        "cache_dir": str(MUSIC_DIR / "cache"),
        "hostname": f"http://{CFG.get('nas_ip', NAS_IP)}",
        "public_port": PUBLIC_PORT,
        "music_list_url": f"{SELF_URL}/playlist.json",
        # setting.json 可能残留旧值，钉死指令通道与轮询开关（见构造参数注释）
        "get_ask_by_mina": False,
        # 为 false 时 poller 空转，口令全不到达
        "enable_pull_ask": True,
    })
    XM.save_cur_config()

    # xiaomusic 日志同时打到 stdout（Docker logs / 应用日志可见）
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [xiaomusic] %(message)s"))
    XM.log.addHandler(h)

    from xiaomusic.api.app import app as xm_app, HttpInit
    HttpInit(XM)
    XM_APP = xm_app


app = FastAPI(title="小爱音箱·飞牛音乐")


# ---------------- 桥接核心状态 ----------------
class State:
    fn: FnMusicClient | None = None
    token: str | None = None
    favorites: list = []
    playlist_songs: int = 0
    last_sync_at: float = 0
    last_push_at: float = 0
    last_error: str = ""
    push_ok: bool = False


STATE = State()


def new_fn_client() -> FnMusicClient:
    return FnMusicClient(
        CFG["fnmusic_base_url"],
        CFG["fnmusic_username"],
        CFG["fnmusic_password"],
    )


# ---------------- 播放列表生成（乱序 + 同名去重） ----------------
def build_playlist(favorites: list) -> list:
    musics = []
    seen = set()
    for t in favorites:
        title = t.get("title") or "未知歌曲"
        artists = t.get("artists") or []
        artist = artists[0].get("name", "") if artists else ""
        album = (t.get("album") or {}).get("name", "")
        name = f"{artist} - {title}" if artist else title
        if name in seen:  # 同名不同专辑：加专辑名区分
            name = f"{name}（{album}）" if album else f"{name}（{t.get('guid', '')[:6]}）"
            if name in seen:
                continue
        seen.add(name)
        musics.append({
            "name": name,
            "url": f"{SELF_URL}/play?guid={t.get('guid', '')}",
        })
    random.shuffle(musics)
    return [{"name": CFG["playlist_name"], "musics": musics}]


# ---------------- 飞牛音乐同步 ----------------
async def do_sync(force: bool = False) -> dict:
    """登录飞牛 → 拉收藏 → 乱序生成歌单 → 推送 xiaomusic"""
    if not CFG["fnmusic_username"] or not CFG["fnmusic_password"]:
        return {"updated": False, "reason": "未配置飞牛账号"}

    STATE.fn = new_fn_client()
    try:
        await STATE.fn.login()
        STATE.token = STATE.fn.token
    except Exception as e:
        STATE.last_error = f"登录失败: {e}"
        log.warning("飞牛登录失败: %s", e)
        return {"updated": False, "error": str(e)}

    try:
        favorites = await STATE.fn.get_favorites()
    except Exception as e:
        STATE.last_error = f"获取收藏失败: {e}"
        log.warning("获取收藏失败: %s", e)
        return {"updated": False, "error": str(e)}

    changed = force or [t.get("guid") for t in favorites] != [
        t.get("guid") for t in STATE.favorites
    ]
    STATE.favorites = favorites
    STATE.last_sync_at = time.time()
    STATE.last_error = ""

    if changed:
        playlist = build_playlist(favorites)
        STATE.playlist_songs = len(playlist[0]["musics"]) if playlist else 0
        log.info("收藏列表变化，重建歌单（%d 首，乱序）", STATE.playlist_songs)
        await push_playlist(playlist)
    return {"updated": changed, "favorites": len(favorites)}


async def push_playlist(playlist: list):
    """把歌单和口令配置推进 xiaomusic（HTTP 内循环）"""
    async with httpx.AsyncClient(base_url=SELF_URL, timeout=15) as cli:
        r = await cli.post("/xm/api/system/modifiysetting", json={
            "music_list_json": json.dumps(playlist, ensure_ascii=False),
            # 口令变体全量推送（每次同步自愈，防止被默认值/旧值覆盖）
            "keywords_playlist": ",".join([
                "播放列表", "播放歌单", "播放收藏", "播放本地",
                "播放本地收藏歌曲", "播放本地收藏", "播放本地歌曲", "播放本地音乐",
                "放几首本地歌", "放几首本地歌曲", "放首本地歌", "放首本地歌曲",
                "随便放几首歌", "随便放首歌", "来几首本地歌", "来几首本地音乐",
            ]),
            # playlocal 是"播放本地下载歌曲"，本方案无下载目录——
            # 改为不常用词，避免其默认值「播放本地歌曲,播放本地音乐」抢占口令
            "keywords_playlocal": "播放下载歌曲",
            # 「播放」放 user_key_word_dict（匹配顺序最后）：
            # 「播放晴天」这类 歌名口令 落到 play 动作，arg1=歌名；
            # 且不会抢占「播放本地歌曲」等更长词（它们在顺序里更靠前）
            "user_key_word_dict": {"播放": "play"},
            "music_list_url": f"{SELF_URL}/playlist.json",
        })
        r.raise_for_status()
        await cli.post("/xm/api/music/refreshlist")
    STATE.last_push_at = time.time()
    STATE.push_ok = True
    log.info("已推送歌单与口令变体并通知 xiaomusic 刷新")


async def sync_loop():
    """定时同步循环"""
    await asyncio.sleep(3)
    while True:
        try:
            if CFG.get("wizard_done"):
                await do_sync()
        except Exception as e:
            STATE.last_error = str(e)
            log.exception("同步循环异常")
        await asyncio.sleep(int(CFG.get("sync_interval", 300)))


@contextlib.asynccontextmanager
async def lifespan(_app):
    # bridge 日志（必须在 uvicorn 启动后挂 handler——uvicorn 的 logging config
    # 会 disable_existing_loggers，module-level 创建的 logger 会被禁用）
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [bridge] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    # 1. 创建 xiaomusic 实例（需要事件循环）并动态 mount 到 /xm/
    #    （uvicorn 保证 lifespan 先于任何请求执行，动态 mount 安全）
    create_xiaomusic()
    _app.mount("/xm", XM_APP)
    # 2. 手动驱动 xiaomusic 的登录+口令轮询（等效其自身 lifespan 的行为）
    asyncio.create_task(XM.run_forever())
    # 3. 播放行为修正：口令重发时重新打乱歌单（见 patch_playlist_behavior）
    patch_playlist_behavior()
    # 3.5 点歌兜底：搜收藏 → 无则飞牛搜索并收藏 → 播放
    patch_play_search()
    # 4. 启动桥接同步循环
    asyncio.create_task(sync_loop())
    log.info("fnmusic-xiaomusic v2 启动完成，端口 %s", BRIDGE_PORT)
    yield
    # 关闭：取消 xiaomusic 后台任务
    with contextlib.suppress(Exception):
        await XM.cancel_all_tasks()


app.router.lifespan_context = lifespan


# ---------------- 播放行为修正 ----------------
def patch_playlist_behavior():
    """修正口令播放行为：任意时刻发「播放本地收藏歌曲」都重新打乱并从头播放。

    xiaomusic 原版行为（device_player.play_music_list）：
      1. update_playlist(force_reshuffle=True) 洗牌时把当前歌"钉"在列表顶端；
      2. music_name 从 device.playlist2music 取"上次播放到的那首"；
      3. _play_internal 里 name 非空直接播那首 —— 暂停后重发口令会"继续"而非重开。
    这里包装 XiaoMusic.play_music_list（command_handler 通过 getattr(实例, 动作)
    动态调用，实例属性 patch 有效）：清空 playlist2music 和 cur_music，
    使 _play_internal 走 check_play_next→_play_next→get_next_music 分支，
    从洗牌后的新列表第一首开始播放。
    """
    if XM is None:
        return
    original = XM.play_music_list

    async def patched_play_music_list(did="", arg1="", **kwargs):
        try:
            list_name = (arg1 or "").split("|")[0].strip()
            dev = XM.device_manager.devices.get(did)
            if dev is not None:
                dev.device.playlist2music.clear()
                dev.device.cur_music = ""
                log.info("[playlist] 口令播放歌单，已重置播放位置（重新洗牌从头播）list=%r", list_name)
        except Exception as e:
            log.warning("[playlist] 重置播放位置失败: %s", e)
        return await original(did=did, arg1=arg1, **kwargs)

    XM.play_music_list = patched_play_music_list
    log.info("[playlist] 播放行为修正已生效")


# ---------------- 点歌兜底：搜索飞牛并自动收藏 ----------------
def _match_favorite(name: str):
    """在收藏里宽松匹配歌名，命中返回可精确搜索的完整歌名，未命中返回 None"""
    n = (name or "").replace(" ", "").lower()
    if not n:
        return None
    for t in STATE.favorites:
        title = (t.get("title") or "").strip()
        artists = " / ".join(
            (a.get("name") or "").strip() for a in (t.get("artists") or []) if a.get("name")
        )
        title_n = title.replace(" ", "").lower()
        artist_n = artists.replace(" ", "").lower()
        if n in title_n or title_n in n or (artist_n and n in artist_n):
            return f"{artists} - {title}" if artists else title
    return None


async def _resolve_song(name: str):
    """解析点歌请求，返回 (mode, arg)：
    - ("ready", 精确歌名)：收藏/本地曲库命中（必要时以收藏为播放手段）
    - ("downloaded", 歌名)：在线歌已下载到本地音乐目录（不进收藏）
    - (None, None)：找不到
    """
    global STATE
    if STATE.fn is None:
        STATE.fn = new_fn_client()
    fn = STATE.fn
    try:
        await fn.login()
        STATE.token = fn.token
    except Exception as e:
        log.warning("[play] 飞牛登录失败: %s", e)
        return None, None

    def _is_online(t):
        return str(t.get("guid") or "").startswith("online:")

    def _full_name(t):
        artists = " / ".join(
            (a.get("name") or "").strip() for a in (t.get("artists") or []) if a.get("name")
        )
        title = (t.get("title") or "").strip()
        return f"{artists} - {title}" if artists else title

    keywords = [name]
    if "的" in name:
        keywords.append(name.replace("的", " "))  # 「周杰伦的晴天」→「周杰伦 晴天」

    online_track = None
    for kw in keywords:
        try:
            tracks = await fn.search_track(kw)
        except Exception as e:
            log.warning("[play] 搜索 %r 失败: %s", kw, e)
            continue
        if not tracks:
            continue
        # 按标题相似度排序（搜索源返回顺序不一定最相关）
        import difflib
        tracks = sorted(
            tracks,
            key=lambda t: difflib.SequenceMatcher(
                None, name.lower(), (t.get("title") or "").lower()
            ).ratio(),
            reverse=True,
        )
        # 优先本地曲库结果（guid 非 online: 前缀）：收藏它使其可被点播
        for t in tracks:
            if not _is_online(t):
                guid = t.get("guid") or ""
                if not guid:
                    continue
                try:
                    await fn.add_favorite(guid)
                    log.info("[play] 本地曲库命中「%s」，已收藏以便点播", _full_name(t))
                except Exception as e:
                    log.warning("[play] 收藏返回异常（可能已收藏）: %s", e)
                try:
                    await do_sync(force=True)
                except Exception as e:
                    log.warning("[play] 同步失败: %s", e)
                exact = _match_favorite(name)
                if exact:
                    return "ready", exact
                # 收藏后仍匹配不到（罕见），用完整名硬试
                return "ready", _full_name(t)
        # 本地没有，记住相似度最高的在线结果，继续下一变体（也可能变体有本地结果）
        if online_track is None:
            for t in tracks:
                if _is_online(t):
                    online_track = t
                    break

    # 本地曲库没有 → 在线歌：经 proxy /track/stream 下载到本地音乐目录（不进收藏）
    if online_track is not None:
        ok = await _download_online_track(online_track)
        if ok:
            return "downloaded", _full_name(online_track)
    log.info("[play] 搜索「%s」无可用结果", name)
    return None, None


def _safe_filename(name: str) -> str:
    import re as _re
    return _re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "unknown"


async def _download_online_track(track: dict) -> bool:
    """把在线歌曲经 proxy 流下载转码为 mp3，存入 xiaomusic 本地音乐目录"""
    guid = track.get("guid") or ""
    title = (track.get("title") or "").strip()
    artists = " / ".join(
        (a.get("name") or "").strip() for a in (track.get("artists") or []) if a.get("name")
    )
    out_path = MUSIC_DIR / (_safe_filename(f"{artists} - {title}") + ".mp3")
    if out_path.exists() and out_path.stat().st_size > 100000:
        log.info("[play] 「%s」已在本地音乐库", out_path.name)
        return True
    log.info("[play] 在线歌曲「%s」开始下载...", title)
    try:
        fn = STATE.fn
        resp = await fn.open_stream(guid)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "192k", str(out_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(180):
                async for chunk in resp.aiter_bytes(65536):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
        finally:
            await resp.aclose()
            try:
                await proc.stdin.drain()
                proc.stdin.close()
            except Exception:
                pass
            await proc.wait()
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 100000:
            log.warning("[play] 下载「%s」失败（rc=%s）", title, proc.returncode)
            out_path.unlink(missing_ok=True)
            return False
        log.info("[play] 下载完成：%s（%.1fMB），刷新本地列表", out_path.name,
                 out_path.stat().st_size / 1048576)
    except Exception as e:
        log.warning("[play] 下载「%s」异常: %s", title, e)
        out_path.unlink(missing_ok=True)
        return False
    # 通知 xiaomusic 重新扫描本地音乐目录
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=30) as cli:
            await cli.post("/xm/api/music/refreshlist")
    except Exception as e:
        log.warning("[play] refreshlist 失败: %s", e)
    return True


def _spawn_auto_playlist_guard(did: str, cmd_snapshot: str):
    """点播的歌自然播完且无新指令时，自动接播收藏随机歌单"""
    async def guard():
        try:
            dev = XM.device_manager.devices.get(did)
            if dev is None:
                return
            started = False
            for _ in range(240):  # 最多监控 20 分钟
                await asyncio.sleep(5)
                if XM.command_handler.last_cmd != cmd_snapshot:
                    return  # 用户发出了新指令，不干预
                if dev.is_playing:
                    started = True
                elif started:
                    break  # 自然播完
            else:
                return
            if XM.command_handler.last_cmd != cmd_snapshot:
                return
            log.info("[play] 点播结束且无新指令，接播收藏随机歌单")
            await XM.play_music_list(did=did, arg1=CFG.get("playlist_name", "本地收藏"))
        except Exception as e:
            log.warning("[play] 自动接歌单异常: %s", e)
    asyncio.create_task(guard())


def patch_play_search():
    """点歌口令：先搜收藏，没有则调飞牛搜索并自动收藏，再播放。

    「播放晴天」经 user_key_word_dict 匹配到 play 动作（arg1=晴天）。
    这里包装 XM.play：先在收藏里宽松匹配（命中则用完整歌名精确播放），
    未命中调 fnmusicext 搜索 → 加入收藏 → 强制同步进歌单 → 播放。
    """
    if XM is None:
        return
    original_play = XM.play

    async def patched_play(did="", arg1="", **kwargs):
        name = (arg1 or "").strip()
        if name:
            cmd_snapshot = XM.command_handler.last_cmd
            exact = _match_favorite(name)
            if exact is None:
                log.info("[play] 收藏中没有「%s」，解析本地曲库/在线下载", name)
                mode, exact = await _resolve_song(name)
                if mode is None:
                    try:
                        dev = XM.device_manager.devices.get(did)
                        if dev is not None:
                            await dev.do_tts(f"没有找到{name}")
                    except Exception as e:
                        log.warning("[play] TTS 失败: %s", e)
                    return
                if exact != name:
                    arg1 = exact  # 用精确歌名确保 xiaomusic 命中
            elif exact != name:
                arg1 = exact
            _spawn_auto_playlist_guard(did, cmd_snapshot)
        return await original_play(did=did, arg1=arg1, **kwargs)

    XM.play = patched_play
    log.info("[play] 点歌搜索兜底已生效")


# ---------------- 根级代理（音箱拉流入口） ----------------
@app.get("/proxy/music")
@app.head("/proxy/music")
async def proxy_music(request: Request):
    """转发到 xiaomusic 的代理端点（xiaomusic 推给音箱的 URL 是 /proxy/music）"""
    target = f"{SELF_URL}/xm/proxy/music"
    if request.url.query:
        target += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection")}
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=180))
    try:
        req = client.build_request(request.method, target, headers=headers)
        resp = await client.send(req, stream=True, follow_redirects=False)

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        passthrough = {k: v for k, v in resp.headers.items()
                       if k.lower() in ("content-type", "content-length",
                                        "content-range", "accept-ranges", "icy-metaint")}
        return StreamingResponse(gen(), status_code=resp.status_code, headers=passthrough)
    except Exception as e:
        await client.aclose()
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------- 音频流代理（桥接 → 飞牛） ----------------
@app.get("/play")
@app.head("/play")
async def play(request: Request, guid: str):
    """带认证请求飞牛音乐取流，跟随 302，流式回传（支持 Range）"""
    if not CFG.get("wizard_done"):
        return JSONResponse({"error": "尚未完成安装向导"}, status_code=503)
    STATE.fn = STATE.fn or new_fn_client()
    range_header = request.headers.get("range")
    try:
        resp = await STATE.fn.open_stream(guid, range_header=range_header)
    except Exception as e:
        log.warning("取流失败 guid=%s: %s", guid, e)
        return JSONResponse({"error": f"取流失败: {e}"}, status_code=502)
    try:
        if resp.status_code >= 400:
            body = await resp.aread()
            return JSONResponse(
                {"error": f"飞牛返回 HTTP {resp.status_code}", "detail": body[:200]},
                status_code=resp.status_code)
        headers = {}
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            v = resp.headers.get(h)
            if v:
                headers[h] = v
        return StreamingResponse(
            resp.aiter_bytes(),
            status_code=resp.status_code,
            media_type=resp.headers.get("Content-Type", "application/octet-stream"),
            headers=headers)
    except Exception as e:
        await resp.aclose()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/playlist.json")
async def playlist_json():
    """歌单 JSON（xiaomusic 的 music_list_url 指向这里）"""
    if not STATE.favorites:
        with contextlib.suppress(Exception):
            await do_sync()
    playlist = build_playlist(STATE.favorites)
    return JSONResponse(playlist)


@app.post("/sync")
async def sync_now():
    result = await do_sync(force=True)
    return {"ret": "OK", **result}


# ---------------- 向导 API ----------------
@app.get("/api/wizard/state")
async def wizard_state():
    """向导当前状态（前端据此渲染步骤）"""
    mi_logged = _mi_logged_in()
    step = 1
    if mi_logged:
        step = 2
    if mi_logged and _devices_selected():
        step = 3
    if CFG.get("wizard_done"):
        step = 4
    return {
        "step": step,
        "nas_ip": CFG.get("nas_ip", NAS_IP),
        "gateway": GATEWAY_IP,
        "public_port": PUBLIC_PORT,
        "mi_account": _mi_setting().get("account", ""),
        "mi_did": _mi_setting().get("mi_did", ""),
        "fnmusic": {k: CFG.get(k) for k in
                    ("fnmusic_base_url", "fnmusic_username")},
        "favorites": len(STATE.favorites),
        "playlist_songs": STATE.playlist_songs,
        "last_error": STATE.last_error,
        "push_ok": STATE.push_ok,
    }


def _mi_setting() -> dict:
    return load_json(CONF_DIR / "setting.json", {})


def _devices_selected() -> bool:
    return bool(_mi_setting().get("mi_did"))


def _mi_logged_in() -> bool:
    """小米登录是否真实有效（mia_session 存在说明 xiaomusic 登录成功）。

    注意：不能只看 setting.json 的 mi_did——迁移过来的配置可能
    账号密码为空、auth.json 的 passToken 已被小米服务端吊销，
    此时配置在但登录是失败的，向导必须让用户重新登录。
    """
    if not _mi_setting().get("mi_did"):
        return False
    try:
        return (XM is not None
                and XM.auth_manager is not None
                and XM.auth_manager.mina_service is not None)
    except Exception:
        return False


@app.post("/api/wizard/mi_login")
async def mi_login(request: Request):
    """Step1a：小米账号密码登录"""
    data = await request.json()
    account, password = data.get("account", "").strip(), data.get("password", "")
    if not account or not password:
        return JSONResponse({"ok": False, "message": "账号和密码不能为空"}, status_code=400)
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=90) as cli:
            r = await cli.post("/xm/savesetting", json={"account": account, "password": password})
            if r.status_code != 200:
                return JSONResponse({"ok": False, "message": f"xiaomusic 返回 {r.status_code}: {r.text[:100]}"})
        # 登录后拉一次设备列表验证
        devices = await _fetch_devices()
        if not devices:
            return JSONResponse({"ok": False, "message": "登录失败或设备列表为空，请检查账号密码"})
        return {"ok": True, "devices": devices}
    except Exception as e:
        log.exception("小米登录异常")
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/api/wizard/qrcode")
async def wizard_qrcode():
    """Step1b：获取扫码登录二维码（复用 xiaomusic 官方实现）"""
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=60) as cli:
            r = await cli.get("/xm/api/get_qrcode")
            data = r.json()
        if data.get("already_logged_in"):
            devices = await _fetch_devices()
            return {"ok": True, "already_logged_in": True, "devices": devices}
        if not data.get("success"):
            return JSONResponse({"ok": False, "message": data.get("message", "二维码生成失败")})
        return {"ok": True, "qrcode_url": data.get("qrcode_url", ""),
                "expire_seconds": data.get("expire_seconds", 120)}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/api/wizard/qrcode_status")
async def wizard_qrcode_status():
    """轮询扫码结果：auth.json 出现有效 token 且 xiaomusic 登录成功即为完成"""
    auth = load_json(CONF_DIR / "auth.json", {})
    if not (auth.get("passToken") and auth.get("userId")):
        return {"ok": True, "status": "waiting"}
    # token 已写入，触发 xiaomusic 用 token 登录
    devices = await _fetch_devices(trigger_login=True)
    if devices:
        return {"ok": True, "status": "success", "devices": devices}
    return {"ok": True, "status": "waiting"}


async def _fetch_devices(trigger_login: bool = False):
    """从小爱侧拉设备列表；trigger_login=True 时先触发一次重新登录"""
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=60) as cli:
            if trigger_login:
                # 空保存触发 reinit → init_all_data → 用 auth.json 的 passToken 登录
                await cli.post("/xm/savesetting", json={})
            r = await cli.get("/xm/getsetting", params={"need_device_list": "true"})
            data = r.json()
        devices = data.get("device_list") or []
        # deviceID -> did 映射（getsetting 的 device_list 里可能没有 did 字段，
        # 而 xiaomusic 的 mi_did 配置需要 did；从 device_manager 拿真实映射）。
        # 扫码登录后立刻拉列表时映射可能还没建立（登录流程是异步的）——
        # 此时主动刷新一次设备信息再取，拿不到 did 的设备由调用方拒绝勾选，
        # 绝不允许退回 deviceID（UUID）——mi_did 存 UUID 会导致口令只对
        # 单台生效甚至完全不生效（音箱走自己的音乐播放）。
        id2did = {}
        try:
            id2did = dict(XM.device_manager.device_id_did or {})
        except Exception:
            pass
        if not id2did and XM is not None and XM.auth_manager is not None:
            try:
                await XM.device_manager.update_device_info(XM.auth_manager)
                id2did = dict(XM.device_manager.device_id_did or {})
            except Exception as e:
                log.warning("刷新设备映射失败: %s", e)
        return [
            {"deviceID": d.get("deviceID"), "name": d.get("name"),
             "hardware": d.get("hardware", ""),
             "did": id2did.get(d.get("deviceID"), d.get("did", ""))}
            for d in devices if d.get("deviceID")
        ]
    except Exception as e:
        log.warning("拉取设备列表失败: %s", e)
        return []


@app.get("/api/wizard/devices")
async def wizard_devices_get():
    """刷新设备列表（登录成功后随时可拉取）"""
    devices = await _fetch_devices()
    return {"ok": True, "devices": devices}


@app.post("/api/wizard/devices")
async def wizard_devices(request: Request):
    """Step2：保存勾选的小爱音箱"""
    data = await request.json()
    dids = [str(d).strip() for d in (data.get("dids") or []) if str(d).strip()]
    if not dids:
        return JSONResponse({"ok": False, "message": "请至少勾选一台音箱"}, status_code=400)
    # did 必须是纯数字（xiaomusic 的 did 形如 865284534）。存进 UUID（deviceID）
    # 会让 mi_did 失配——口令只对单台生效甚至全部走音箱自己的播放。
    bad = [d for d in dids if not d.isdigit()]
    if bad:
        return JSONResponse(
            {"ok": False,
             "message": "设备标识异常（取到了 UUID 而非 did），请返回上一步重新获取设备列表"},
            status_code=400)
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=90) as cli:
            r = await cli.post("/xm/savesetting", json={"mi_did": ",".join(dids)})
            if r.status_code != 200:
                return JSONResponse({"ok": False, "message": f"保存失败: {r.text[:100]}"})
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/api/wizard/scan_fnmusic")
async def scan_fnmusic():
    """Step3a：自动扫描飞牛音乐服务（容器内经默认网关访问宿主）"""
    candidates = [
        f"http://{GATEWAY_IP}:5666" if GATEWAY_IP else "",
        f"http://{CFG.get('nas_ip', NAS_IP)}:5666",
        "http://localhost:5666",
    ]
    found = []
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as cli:
        for url in candidates:
            if not url or url in found:
                continue
            try:
                r = await cli.get(f"{url}/music/api/v1/initialization/state")
                if r.status_code == 200:
                    body = r.json()
                    if body.get("code") == 0 or "initialized" in body:
                        found.append(url)
            except Exception:
                pass
    return {"ok": True, "found": found, "scanned": [c for c in candidates if c],
            "gateway": GATEWAY_IP}


@app.post("/api/wizard/fnmusic")
async def wizard_fnmusic(request: Request):
    """Step3b：保存飞牛音乐配置并验证登录"""
    data = await request.json()
    base_url = (data.get("base_url") or "").strip().rstrip("/")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not base_url or not username or not password:
        return JSONResponse({"ok": False, "message": "请填写完整的地址、账号和密码"}, status_code=400)
    # 验证登录
    try:
        client = FnMusicClient(base_url, username, password)
        await client.login()
        favorites = await client.get_favorites()
        await client.aclose()
    except Exception as e:
        return JSONResponse({"ok": False, "message": f"登录失败: {e}"})
    # 保存配置，标记向导完成
    CFG.update({
        "fnmusic_base_url": base_url,
        "fnmusic_username": username,
        "fnmusic_password": password,
        "nas_ip": data.get("nas_ip") or CFG.get("nas_ip", NAS_IP),
        "wizard_done": True,
    })
    save_json(BRIDGE_CONFIG_FILE, CFG)
    # 立即同步一次
    asyncio.create_task(do_sync(force=True))
    return {"ok": True, "favorites": len(favorites)}


@app.post("/api/wizard/reset")
async def wizard_reset():
    """完全重置向导：清小米登录态（mi_did + 登录令牌）与飞牛配置。

    调用方（WebUI）随后会重启本进程——内存里的登录会话也随之清零，
    重启后 wizard_state 回到第一步，用户重新登录小米 → 勾选设备 → 填飞牛。
    """
    # mi_did/account 经 xiaomusic 自己的 API 清（内存与 setting.json 同步，
    # 避免直接改文件被进程退出时的写回覆盖）
    try:
        async with httpx.AsyncClient(base_url=SELF_URL, timeout=15) as cli:
            await cli.post("/xm/savesetting", json={"mi_did": "", "account": ""})
    except Exception as e:
        log.warning("reset: 清 mi_did/account 失败: %s", e)
    # 登录令牌删除：进程重启后无令牌可登，_mi_logged_in() 为 False，向导回第一步
    for f in (CONF_DIR / "auth.json", CONF_DIR / ".mi.token"):
        with contextlib.suppress(Exception):
            f.unlink(missing_ok=True)
    CFG.update({"fnmusic_username": "", "fnmusic_password": "", "wizard_done": False})
    save_json(BRIDGE_CONFIG_FILE, CFG)
    log.info("向导已完全重置（小米登录态与飞牛配置已清）")
    return {"ok": True}


# ---------------- 状态页与 UI ----------------
@app.get("/health")
@app.get("/healthz")
async def health():
    return {
        "ok": True,
        "token": STATE.token is not None,
        "mi_logged": _mi_logged_in(),
        "favorites": len(STATE.favorites),
        "playlist": CFG["playlist_name"],
        "playlist_songs": STATE.playlist_songs,
        "last_sync_at": STATE.last_sync_at,
        "last_push_at": STATE.last_push_at,
        "push_ok": STATE.push_ok,
        "wizard_done": CFG.get("wizard_done", False),
        "last_error": STATE.last_error,
        "nas_ip": CFG.get("nas_ip", NAS_IP),
        "public_port": PUBLIC_PORT,
        "fnmusic_base_url": CFG.get("fnmusic_base_url", ""),
        "gateway": GATEWAY_IP,
        # 指令轮询开关：false 时 poller 空转，口令全不到达（静默故障，必须暴露）
        "pull_ask_on": bool(XM and getattr(XM.config, "enable_pull_ask", False)),
        # 指令拉取通道：xiaoai=对话记录 API（可用）/ mina=ubus（部分固件返回空壳）
        "ask_channel": ("mina" if (XM and getattr(XM.config, "get_ask_by_mina", False))
                        else "xiaoai"),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "static" / "wizard.html").read_text(encoding="utf-8")
    logo = Path(__file__).parent / "static" / "logo.png"
    if logo.exists():
        b64 = base64.b64encode(logo.read_bytes()).decode()
        html = html.replace("__LOGO_B64__", b64)
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
