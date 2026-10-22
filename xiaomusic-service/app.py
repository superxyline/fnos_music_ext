"""fnmusic-xiaomusic 桥接（fnmusic-ext 内置版）

作为 fnmusic-ext fnmusic-sources 容器内的第 5 个 supervisor 进程运行
（默认 8005，宿主映射 8775）：

  - 口令引擎（native/ 目录）：复用 xiaomusic 的登录/设备/轮询/播放组件
    （miservice/AuthManager/DeviceManager/ConversationPoller/MusicLibrary/
    XiaoMusicDevice），编排与命令分发为自研实现；不挂载其 api 子应用
  - 桥接飞牛音乐：收藏同步、歌单生成、带认证音频流代理
  - 配置入口：fnmusic-ext 管理台「小爱音箱」页（WebUI 代理本服务的向导 API）；
    本服务自带的 / 页面保留作旁路调试入口

数据目录 /data/xiaomusic/{conf,music,bridge}（sources-data 卷内独立子目录）。
推送给音箱的外部地址 = http://{nas_ip}:{PUBLIC_PORT}（宿主映射 8775）。
飞牛音乐地址默认取容器默认网关（宿主 docker0），音箱访问地址由用户在
管理台配置页确认（NAS 局域网 IP）。
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
from fastapi.staticfiles import StaticFiles

from fnmusic_client import FnMusicClient, sign_authx
from audio_id_resolver import (
    AudioIdCache,
    resolve_audio_ids,
    search_audio_id,
    DEFAULT_AUDIO_ID,
)
from native.keywords import (
    KEYWORDS_PLAYLIST_DEFAULT,
    KEYWORDS_PLAYLOCAL_DEFAULT,
    USER_KEY_WORD_DICT_DEFAULT,
)

# ---------------- 配置 ----------------
# 数据目录独立子目录：避免与 musicbox/lxmusic 等服务的数据混居（同卷 /data）
DATA_DIR = Path(os.getenv("XIAOMUSIC_DATA_DIR", "/data/xiaomusic"))
CONF_DIR = DATA_DIR / "conf"          # xiaomusic 配置（setting.json / auth.json / .mi.token）
MUSIC_DIR = DATA_DIR / "music"        # xiaomusic 音乐目录（本方案只用网络歌单，保持为空）
BRIDGE_DIR = DATA_DIR / "bridge"      # 桥接数据
WIZARD_STATE_FILE = BRIDGE_DIR / "wizard_state.json"
BRIDGE_CONFIG_FILE = BRIDGE_DIR / "config.json"
AUDIO_ID_CACHE_FILE = BRIDGE_DIR / "audio_id_cache.json"

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
    "playlist_seq_name": "本地收藏（顺序）",
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

# 小米曲库 audio_id 缓存（X4B 等带屏设备屏幕显示的元数据来源，见 audio_id_resolver）
AUDIO_ID_CACHE = AudioIdCache(AUDIO_ID_CACHE_FILE)

# ---------------- 口令引擎 ----------------
# 注意：引擎构造内部会创建 aiohttp.ClientSession，必须在有事件循环的
# 异步上下文中执行，因此延迟到 FastAPI lifespan 里创建（uvicorn 保证
# lifespan 先于任何请求完成）。

XM = None   # 口令引擎实例（NativeCore）


def create_native():
    """创建口令引擎（复用 xiaomusic 的登录/设备/轮询/播放组件，自建编排与分发）。

    - 不挂载 xiaomusic 的 api 子应用（/xm/*），向导/歌单/取流全走桥接自身端点
    - 配置从 conf/setting.json 读取
    - web_music_proxy=False：音箱直连桥接 /play 取流，不经 xiaomusic 代理
    必须在有事件循环的异步上下文调用（AuthManager 内部建 aiohttp.ClientSession）。
    """
    global XM
    for d in (CONF_DIR, MUSIC_DIR, MUSIC_DIR / "tmp", MUSIC_DIR / "download",
              MUSIC_DIR / "cache", BRIDGE_DIR):
        d.mkdir(parents=True, exist_ok=True)

    from native.config import build_config
    from native.core import NativeCore

    settings = load_json(CONF_DIR / "setting.json", {})
    cfg = build_config(
        conf_dir=CONF_DIR,
        music_dir=MUSIC_DIR,
        nas_ip=CFG.get("nas_ip", NAS_IP),
        public_port=PUBLIC_PORT,
        settings=settings,
    )
    XM = NativeCore(cfg, log)


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
def _play_entry_base() -> str:
    """歌单条目基地址（音箱拉流用）。

    web_music_proxy=False，条目 URL 原样发给音箱，必须用**对外可达**地址
    http://{nas_ip}:{public_port}——127.0.0.1:8005 是容器内环地址，音箱拿到
    只会连它自己（2026-10-19 实测播放无声）。
    """
    return f"http://{CFG.get('nas_ip', NAS_IP)}:{PUBLIC_PORT}"


def build_playlist(favorites: list) -> list:
    """歌单（两份同曲、顺序不同）：
    - 「本地收藏」：乱序——口令「播放收藏」随机听（历史行为）
    - 「本地收藏（顺序）」：按收藏进入的先后排列——口令「顺序播放本地歌曲」
      从第一首开始顺序听（2026-10-22 用户需求）
    保序歌单必须在乱序歌单之后：MusicLibrary 按 JSON 顺序入库，
    「播放收藏」空参数模糊匹配取第一个歌单，不能因此变成顺序版。
    """
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
            "url": f"{_play_entry_base()}/play?guid={t.get('guid', '')}",
        })
    shuffled = list(musics)
    random.shuffle(shuffled)
    return [
        {"name": CFG["playlist_name"], "musics": shuffled},
        {"name": CFG["playlist_seq_name"], "musics": musics},
    ]


# ---------------- 飞牛音乐同步 ----------------
async def do_sync(force: bool = False) -> dict:
    """登录飞牛 → 拉收藏 → 生成歌单（乱序+保序两份）→ 推送 xiaomusic"""
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
        log.info("收藏列表变化，重建歌单（%d 首：乱序 + 保序两份）", STATE.playlist_songs)
        await push_playlist(playlist)
        _spawn_audio_id_resolve(playlist)
    return {"updated": changed, "favorites": len(favorites)}


async def push_playlist(playlist: list):
    """把歌单和口令配置推进引擎（xiaomusic 走 HTTP 内循环，native 走直接调用）"""
    # 口令变体全量推送（每次同步自愈，防止被默认值/旧值覆盖）；
    # 默认值单一来源：native/keywords.py（两个引擎共用）
    keywords_playlist = KEYWORDS_PLAYLIST_DEFAULT
    keywords_playlocal = KEYWORDS_PLAYLOCAL_DEFAULT
    user_key_word_dict = dict(USER_KEY_WORD_DICT_DEFAULT)
    music_list_url = f"{SELF_URL}/playlist.json"

    # 单一 native 引擎：歌单走 music_list_json（MusicLibrary 直接消费）+
    # 口令变体经 Config.update_config 重建；setting.json 落盘保持配置互通
    music_list_json = json.dumps(playlist, ensure_ascii=False)
    XM.push_config(
        music_list_json=music_list_json,
        keywords_playlist=keywords_playlist,
        keywords_playlocal=keywords_playlocal,
        user_key_word_dict=user_key_word_dict,
        music_list_url=music_list_url,
        playlist_seq_name=CFG.get("playlist_seq_name", ""),
    )
    _save_engine_setting({"music_list_json": music_list_json})
    STATE.last_push_at = time.time()
    STATE.push_ok = True
    log.info("已推送歌单与口令变体并通知引擎刷新")


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
    # 1. 创建引擎实例（需要事件循环，lifespan 保证先于请求执行）
    create_native()
    log.info("引擎: native（复用 xiaomusic 登录/设备/轮询/播放组件，自建编排）")
    # 2. 手动驱动登录+口令轮询（等效 xiaomusic 自身 lifespan 的行为）
    asyncio.create_task(XM.run_forever())
    # 3. 播放行为修正：口令重发时重新打乱歌单（见 patch_playlist_behavior）
    patch_playlist_behavior()
    # 3.5 点歌兜底：搜收藏 → 无则飞牛搜索并收藏 → 播放
    patch_play_search()
    # 3.6 轮询看门狗：登录失效时停止撞击小米接口（避免风控恶性循环）
    patch_poll_watchdog()
    # 3.7 audio_id 解析：带屏设备屏幕显示正确的歌曲信息（见 patch docstring）
    patch_audio_id_resolver()
    # 3.8 轮询间隔守卫：登录态变化时把 pull_ask_sec 在 1s / 60s 之间切换
    asyncio.create_task(poll_guard_loop())
    # 3.9 本地音乐文件服务（/music/*）：
    #     xiaomusic 的 _get_file_url 会拼 http://{nas_ip}:{public_port}/music/...
    #     发给音箱——edge-tts 确认音在 music/tmp/ 下、点歌下载的本地歌曲在
    #     music/ 根下，都走这个 URL。桥接必须提供该路由，否则音箱拉不到音频，
    #     小爱会提示"播放服务遇到问题"。
    #     （v3.3.0 起潜伏的 bug：2026-10-18~19 日志里 26 次请求全部 404，
    #      xiaomusic/native 两引擎同病——引擎只负责拼 URL，路由在桥接侧）
    _app.mount("/music", StaticFiles(directory=str(MUSIC_DIR)), name="music")
    # 4. 启动桥接同步循环
    asyncio.create_task(sync_loop())
    log.info("fnmusic-xiaomusic v2 启动完成，端口 %s", BRIDGE_PORT)
    yield
    # 关闭：取消 xiaomusic 后台任务
    with contextlib.suppress(Exception):
        await XM.cancel_all_tasks()


app.router.lifespan_context = lifespan


# ---------------- 口令轮询看门狗：登录失效时停止撞击小米接口 ----------------
def patch_poll_watchdog():
    """小米登录不可用时跳过对话记录拉取，并降频等待重新登录。

    背景（2026-10-19 实测）：登录失效后 poll_latest_ask 仍按 pull_ask_sec（1s）
    遍历每台设备请求对话记录。此时设备列表为空、hardware 取不到，每次必然 400
    （实测 10 分钟 6396 次请求），形成「失效 → 狂撞接口 → 风控加重 → 更难恢复」
    的恶性循环，同时刷满日志、浪费 NAS 资源。

    做法：包装 ConversationPoller.get_latest_ask_from_xiaoai——
      - auth_manager.mina_service 为空（登录未成功）→ 直接返回，一个请求都不发，
        并把 pull_ask_sec 抬到 60，让外层循环低频空转；
      - 登录可用 → pull_ask_sec 恢复 1s，正常拉取口令。
    重新扫码后 mina_service 会被赋值，最多 60 秒内自动恢复，无需重启进程。
    """
    if XM is None:
        return
    poller = getattr(XM, "conversation_poller", None)
    if poller is None:
        return
    original = poller.get_latest_ask_from_xiaoai
    state = {"degraded": None}   # None=未初始化，True=降频中，False=正常

    async def patched_get_latest_ask_from_xiaoai(session, device_id):
        healthy = bool(XM.auth_manager and XM.auth_manager.mina_service)
        if state["degraded"] != (not healthy):
            state["degraded"] = not healthy
            if healthy:
                XM.config.pull_ask_sec = 1
                log.info("[poll] 小米登录恢复，指令轮询回到 1s")
            else:
                XM.config.pull_ask_sec = 60
                log.warning(
                    "[poll] 小米登录不可用，暂停拉取对话记录"
                    "（不再请求小米接口，避免风控；每 60s 检查一次登录态）"
                )
        if not healthy:
            return None
        return await original(session, device_id)

    poller.get_latest_ask_from_xiaoai = patched_get_latest_ask_from_xiaoai
    log.info("[poll] 轮询看门狗已生效")


def poll_degraded() -> bool:
    """指令轮询是否因登录不可用而降频（health 与前端据此提示）"""
    if XM is None:
        return False
    am = getattr(XM, "auth_manager", None)
    return bool(am is not None and am.mina_service is None)


async def poll_guard_loop():
    """周期性核对轮询间隔，与登录态保持一致。

    为什么需要它：登录失败后 device_manager.device_id_did 会变空，
    poll_latest_ask 没有设备可遍历，get_latest_ask_from_xiaoai（看门狗包装的那层）
    根本不会被调用——此时 pull_ask_sec 会一直停在 1，health 观测不准。
    本任务每 10 秒核对一次：登录可用→1s，不可用→60s，变化时才打日志。
    与 patch_poll_watchdog 分工：它拦请求，这里维护间隔与可观测性。
    """
    while True:
        await asyncio.sleep(10)
        try:
            if XM is None:
                continue
            healthy = bool(XM.auth_manager and XM.auth_manager.mina_service)
            want = 1 if healthy else 60
            if getattr(XM.config, "pull_ask_sec", None) != want:
                XM.config.pull_ask_sec = want
                if healthy:
                    log.info("[poll] 小米登录恢复，轮询间隔回到 %ss", want)
                else:
                    log.warning("[poll] 小米登录不可用，轮询间隔降为 %ss（不请求小米接口）", want)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("[poll] 轮询守护检查失败: %s", e)


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
    另：随机歌单播放前把设备切回随机模式——「顺序播放本地歌曲」会把设备切到
    顺序模式（保序歌单不被洗牌），若不切回，之后的「播放收藏」在顺序模式下
    直接取推送时 baked 的乱序、不再每次重洗，"重新打乱"的承诺就丢了。
    """
    if XM is None:
        return
    try:
        from xiaomusic.const import PLAY_TYPE_RND
    except ImportError:  # 无 xiaomusic 环境（本地测试）：上游常量值兜底
        PLAY_TYPE_RND = 2
    original = XM.play_music_list

    async def patched_play_music_list(did="", arg1="", **kwargs):
        try:
            list_name = (arg1 or "").split("|")[0].strip()
            dev = XM.device_manager.devices.get(did)
            if dev is not None:
                dev.device.playlist2music.clear()
                dev.device.cur_music = ""
                if getattr(dev.device, "play_type", None) != PLAY_TYPE_RND:
                    # 不播报模式切换（dotts=False）：口令播放歌单本就安静
                    await dev.set_play_type(PLAY_TYPE_RND, False)
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
    - ("ready", 精确歌名)：收藏/本地曲库命中（本地曲库只注入内存播放列表，
      绝不写入飞牛收藏——点歌≠收藏）
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
        # 优先本地曲库结果（guid 非 online: 前缀）：注入内存播放列表直接播，
        # 绝不写入飞牛收藏（点歌≠收藏；旧实现 add_favorite 会污染用户收藏）
        for t in tracks:
            if not _is_online(t):
                guid = t.get("guid") or ""
                if not guid:
                    continue
                full = _inject_to_playlist(_full_name(t), guid)
                if not full:
                    continue
                log.info("[play] 本地曲库命中「%s」，已注入播放列表（不入收藏）", full)
                return "ready", full
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


def _inject_to_playlist(name: str, guid: str) -> str:
    """把点歌曲目注入内存播放列表（不写 setting.json、不改飞牛收藏）。

    xiaomusic 的播放要求歌名在 music_library.all_music / music_list 里。
    历史做法是 add_favorite 后强制同步——会污染用户收藏（用户反馈：点歌
    ≠收藏）。这里直接改内存结构：当下可点播、下一首可接续（update_playlist
    从 music_list 取歌）；下次歌单重建（收藏变化/刷新列表/点歌下载）自动
    清掉，不落盘、不进收藏。返回规范化歌名（失败返回空串）。

    注入后 is_web_music(name) 为真（all_music[name] 是 http URL），
    播放/时长/取流走与收藏条目完全相同的代码路径。
    """
    lib = getattr(XM, "music_library", None)
    name = (name or "").strip()
    if lib is None or not name:
        return ""
    url = f"{_play_entry_base()}/play?guid={guid}"
    lib.all_music[name] = url
    index = getattr(lib, "_extra_index_search", None)
    if isinstance(index, dict):
        index[url] = name
    list_name = CFG.get("playlist_name", "本地收藏")
    bucket = lib.music_list.setdefault(list_name, [])
    if name not in bucket:
        bucket.append(name)
    return name


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
    # 通知引擎重新扫描本地音乐目录
    try:
        await XM.refresh_music_list()
    except Exception as e:
        log.warning("[play] refreshlist 失败: %s", e)
    return True


def _spawn_auto_playlist_guard(did: str, cmd_snapshot: str):
    """点播的歌自然播完且无新指令时，自动接播收藏随机歌单"""

    def _last_cmd() -> str:
        return getattr(XM, "last_cmd", "")

    async def guard():
        try:
            dev = XM.device_manager.devices.get(did)
            if dev is None:
                return
            started = False
            for _ in range(240):  # 最多监控 20 分钟
                await asyncio.sleep(5)
                if _last_cmd() != cmd_snapshot:
                    return  # 用户发出了新指令，不干预
                if dev.is_playing:
                    started = True
                elif started:
                    break  # 自然播完
            else:
                return
            if _last_cmd() != cmd_snapshot:
                return
            log.info("[play] 点播结束且无新指令，接播收藏随机歌单")
            await XM.play_music_list(did=did, arg1=CFG.get("playlist_name", "本地收藏"))
        except Exception as e:
            log.warning("[play] 自动接歌单异常: %s", e)
    asyncio.create_task(guard())


def patch_play_search():
    """点歌口令：先搜收藏，没有则调飞牛搜索（本地曲库注入播放列表 / 在线歌下载），
    再播放——点歌绝不写入飞牛收藏。

    「播放晴天」经 user_key_word_dict 匹配到 play 动作（arg1=晴天）。
    这里包装 XM.play：先在收藏里宽松匹配（命中则用完整歌名精确播放），
    未命中调 fnmusicext 搜索：
      - 本地曲库命中 → 注入内存播放列表直接播（v3.3.5 起，旧版会 add_favorite）
      - 在线歌命中 → ffmpeg 下载到本地音乐目录后播（不入收藏）
    """
    if XM is None:
        return
    original_play = XM.play

    async def patched_play(did="", arg1="", **kwargs):
        name = (arg1 or "").strip()
        if name:
            cmd_snapshot = getattr(XM, "last_cmd", "")
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


# ---------------- audio_id 解析：带屏设备屏幕显示正确的歌曲信息 ----------------
def _spawn_audio_id_resolve(playlist: list):
    """歌单重建后后台预解析条目的 audio_id（不等结果，播放时缓存即生效）。

    只解析未缓存的（增量）；登录不可用则跳过，等下次同步补。
    """
    names = []
    for group in playlist or []:
        for m in (group.get("musics") or []):
            n = (m.get("name") or "").strip()
            if n:
                names.append(n)
    # 两份歌单（乱序/保序）曲目相同：去重后再解析，避免预解析数量翻倍
    names = list(dict.fromkeys(names))
    if not names:
        return

    async def _task():
        mina = None
        if XM is not None and XM.auth_manager is not None:
            mina = XM.auth_manager.mina_service
        if mina is None:
            log.info("[audio_id] 小米未登录，跳过预解析（播放时按需解析）")
            return
        log.info("[audio_id] 开始预解析（%d 首待查）", len(names))
        try:
            stats = await resolve_audio_ids(mina, names, AUDIO_ID_CACHE)
            pruned = AUDIO_ID_CACHE.prune(names)
            AUDIO_ID_CACHE.save()
            log.info("[audio_id] 预解析完成 %s（清理过期缓存 %d 条）", stats, pruned)
        except Exception as e:
            log.warning("[audio_id] 预解析任务异常: %s", e)

    asyncio.create_task(_task())


def patch_audio_id_resolver():
    """包装上游 XiaoMusicDevice._get_audio_id，修掉带屏设备"屏幕显示错歌"。

    原理（2026-10-20 源码级定位）：X4B 等 NEED_USE_PLAY_MUSIC_API 设备走
    mina ubus player_play_music，屏幕歌名/歌手/封面/歌词全由 audio_id 的
    曲库记录决定（播放内容则是桥接 URL，两者无绑定）。上游 _get_audio_id
    按「歌名-歌手」拆名，而桥接歌单名是「歌手 - 歌名」（artist 在前），
    拆分后与曲库歌名永远不相等 → 掉 songList[0] 兜底：蒙对显示正确，
    蒙错屏幕显示另一首歌（用户反馈"偶尔显示错误"的根因）。

    本包装：仅对真正走 play_music 路径的设备（名单内或 use_music_api/
    continue_play）生效，行为 = 缓存优先 → 规范化多候选搜索 → 仍不中则
    上游原逻辑兜底；解析结果写缓存（负缓存防反复打接口）。
    """
    if XM is None:
        return
    try:
        from xiaomusic.const import NEED_USE_PLAY_MUSIC_API
        from xiaomusic.device_player import XiaoMusicDevice
    except ImportError:
        log.info("[audio_id] 无 xiaomusic 环境，跳过 audio_id 解析包装")
        return
    original = XiaoMusicDevice._get_audio_id

    async def patched(self, name):
        cfg = self.config
        fixed = str(getattr(cfg, "use_music_audio_id", None) or DEFAULT_AUDIO_ID)
        name = (name or "").strip()
        if not name:
            return fixed
        use_api = bool(getattr(cfg, "use_music_api", False)
                       or getattr(cfg, "continue_play", False))
        hardware = getattr(self, "hardware", "") or ""
        if not (use_api or hardware in NEED_USE_PLAY_MUSIC_API):
            # 非 play_music 路径的设备屏幕不显示卡片，照搬上游：用配置值
            return fixed
        # 1. 缓存优先（"" = 负缓存：查过曲库没有）
        cached = AUDIO_ID_CACHE.get(name)
        if cached is not None:
            return cached or fixed
        # 2. 规范化多候选搜索
        audio_id = ""
        try:
            mina = getattr(self.auth_manager, "mina_service", None)
            if mina is not None:
                audio_id = await search_audio_id(mina, name)
        except Exception as e:
            log.warning("[audio_id] 解析 %r 失败，回退上游逻辑: %s", name, e)
        # 3. 仍不中：保留上游宽松兜底语义（异常时也不阻断播放）
        if not audio_id:
            try:
                audio_id = await original(self, name)
            except Exception as e:
                log.debug("[audio_id] 上游兜底失败: %s", e)
                audio_id = ""
        audio_id = str(audio_id or "")
        AUDIO_ID_CACHE.set(name, audio_id)
        AUDIO_ID_CACHE.save()
        log.info("[audio_id] 解析 %r -> %s", name, audio_id or "默认占位")
        return audio_id or fixed

    XiaoMusicDevice._get_audio_id = patched
    log.info("[audio_id] 屏幕音频ID解析已生效（缓存 %s）", AUDIO_ID_CACHE.stats())


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


def _save_engine_setting(data: dict):
    """写 conf/setting.json 并同步内存 Config（引擎配置的统一落盘通道）。"""
    settings = load_json(CONF_DIR / "setting.json", {})
    settings.update(data)
    save_json(CONF_DIR / "setting.json", settings)
    if XM is not None:
        with contextlib.suppress(Exception):
            XM.config.update_config(data)


@app.post("/api/wizard/mi_login")
async def mi_login(request: Request):
    """Step1a：小米账号密码登录"""
    data = await request.json()
    account, password = data.get("account", "").strip(), data.get("password", "")
    if not account or not password:
        return JSONResponse({"ok": False, "message": "账号和密码不能为空"}, status_code=400)
    try:
        _save_engine_setting({"account": account, "password": password})
        # fetch_devices(trigger_login=True) 内部会 init_all_data(force_login=True)
        devices = await XM.fetch_devices(trigger_login=True)
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
        # 直调 MiJiaAPI；拿到 lp 后后台轮询扫码状态，成功后 auth.json 落盘
        qr = XM.qrlogin.get_qrcode()
        if qr is False:
            devices = await XM.fetch_devices()
            return {"ok": True, "already_logged_in": True, "devices": devices}
        lp = (qr or {}).get("lp", "")
        if lp:
            XM.qrlogin.start_status_poll(lp)
        image_url = XM.qrlogin.image_url(qr or {})
        if not image_url:
            return JSONResponse({"ok": False, "message": "二维码生成失败"})
        return {"ok": True, "qrcode_url": image_url, "expire_seconds": 120}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/api/wizard/qrcode_status")
async def wizard_qrcode_status():
    """轮询扫码结果：auth.json 出现有效 token 且引擎登录成功即为完成"""
    auth = load_json(CONF_DIR / "auth.json", {})
    if not (auth.get("passToken") and auth.get("userId")):
        return {"ok": True, "status": "waiting"}
    # token 已写入，触发引擎用 token 登录
    devices = await _fetch_devices(trigger_login=True)
    if devices:
        return {"ok": True, "status": "success", "devices": devices}
    return {"ok": True, "status": "waiting"}


async def _fetch_devices(trigger_login: bool = False):
    """从小爱侧拉设备列表；trigger_login=True 时先触发一次重新登录。

    直接走 AuthManager/DeviceManager（内部含 did 映射刷新）。绝不允许用
    deviceID（UUID）充当 did——mi_did 存 UUID 会导致口令只对单台生效甚至
    完全不生效（音箱走自己的播放）。
    """
    try:
        return await XM.fetch_devices(trigger_login=trigger_login)
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
        _save_engine_setting({"mi_did": ",".join(dids)})
        # 设备列表由 DeviceManager 按 mi_did 过滤，立即刷新一次
        await XM.device_manager.update_device_info(XM.auth_manager)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=500)


@app.get("/api/wizard/scan_fnmusic")
async def scan_fnmusic():
    """Step3a：自动扫描飞牛音乐服务（容器内经默认网关访问宿主）"""
    hosts = [GATEWAY_IP, CFG.get("nas_ip", NAS_IP)]
    candidates = [f"http://{h}:5666" for h in hosts if h]
    candidates.append("http://localhost:5666")
    found = []
    seen = set()
    # follow_redirects=True + verify=False：fnOS 默认强制 HTTPS
    # （http://:5666 → 302 → https://:5667 自签证书）。不跟随重定向 +
    # 严格 TLS 校验时，强制 HTTPS 的 fnOS 永远扫不到（issue #5 复现）；
    # 本探测只读 initialization/state，不携带任何凭据，跟随重定向无泄漏风险。
    # trust_env=False：不继承容器里的 HTTP_PROXY（在线源代理），内网直连。
    async with httpx.AsyncClient(timeout=5, follow_redirects=True,
                                 verify=False, trust_env=False) as cli:
        for url in candidates:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                r = await cli.get(f"{url}/music/api/v1/initialization/state")
                if r.status_code == 200:
                    body = r.json()
                    if body.get("code") == 0 or "initialized" in body:
                        # 跟随重定向后的最终源站（强制 HTTPS 时即 https://:5667），
                        # 存这个地址 Wizard 第 3 步直接用，不必再吃一次 302
                        final = urllib.parse.urlparse(str(r.url))
                        resolved = f"{final.scheme}://{final.netloc}"
                        if resolved not in found:
                            found.append(resolved)
            except Exception:
                pass
    return {"ok": True, "found": found, "scanned": candidates,
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
    # 保存配置，标记向导完成。
    # base_url 取登录后的实际值：强制 HTTPS 环境下 FnMusicClient 会在首次
    # 登录时把 http://:5666 自动升级为 https://:5667（issue #5），落盘真实
    # 地址，重启后不再探测、管理台显示的也是实际在用的地址
    CFG.update({
        "fnmusic_base_url": client.base_url,
        "fnmusic_username": username,
        "fnmusic_password": password,
        "nas_ip": data.get("nas_ip") or CFG.get("nas_ip", NAS_IP),
        "wizard_done": True,
    })
    save_json(BRIDGE_CONFIG_FILE, CFG)
    # 立即同步一次
    asyncio.create_task(do_sync(force=True))
    return {"ok": True, "favorites": len(favorites), "base_url": client.base_url}


@app.post("/api/wizard/reset")
async def wizard_reset():
    """完全重置向导：清小米登录态（mi_did + 登录令牌）与飞牛配置。

    调用方（WebUI）随后会重启本进程——内存里的登录会话也随之清零，
    重启后 wizard_state 回到第一步，用户重新登录小米 → 勾选设备 → 填飞牛。
    """
    # mi_did/account 经引擎自己的配置通道清（内存与 setting.json 同步，
    # 避免直接改文件被进程退出时的写回覆盖）
    try:
        _save_engine_setting({"mi_did": "", "account": ""})
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
        # 登录不可用导致轮询降频：此时不再请求小米接口（避免 400 风暴加重风控）
        "poll_degraded": poll_degraded(),
        # 当前实际轮询间隔（秒）：正常 1，登录失效降频 60
        "pull_ask_sec": (getattr(XM.config, "pull_ask_sec", None) if XM else None),
        # 带屏设备 audio_id 缓存：resolved=已匹配曲库ID，negative=查过没有
        "audio_id_cache": AUDIO_ID_CACHE.stats(),
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
