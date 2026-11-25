"""native 引擎核心（NativeCore）。

复用 xiaomusic 上游组件，自建编排：
- AuthManager         登录（账号密码 / auth.json passToken / fresh-session 重登）
- DeviceManager       设备列表与 did 映射（内部装配 XiaoMusicDevice）
- ConversationPoller  对话记录轮询（口令入口，run_conversation_loop 回调）
- MusicLibrary        歌单机制（music_list_url 网络歌单 + 本地目录扫描）
- XiaoMusicDevice     单设备播放控制（mina 播放/TTS/下一首/停止小爱）

NativeCore 自身充当上游组件需要的 "xiaomusic 实例"（shim）：
DeviceManager(xiaomusic=self) 与 XiaoMusicDevice(xiaomusic=self) 实际只用到
config / log / auth_manager / device_manager / music_library / event_bus /
analytics / js_plugin_manager / do_tts / handle_fatal_error / add_singer_song
（2026-10-19 对 xiaomusic 0.6.1 源码逐行核实，见 HANDOFF_2026-10-19.md §P1a）。

与 xiaomusic 引擎的差异：
- 不挂载 /xm/* HTTP api：向导、歌单、取流全部走桥接自身端点
- 不启用 online_music / js_plugin / analytics / 文件监控 / 定时任务
- web_music_proxy 默认 False：音箱直连桥接 /play 取流（不经 xiaomusic 代理）
- 与 xiaomusic 引擎共用 /data/xiaomusic/conf（auth.json/setting.json），
  切换引擎不丢登录态与设备选择
"""
import asyncio
import logging
import random
import re
import time
from pathlib import Path

from xiaomusic.auth import AuthManager
from xiaomusic.config import Config
from xiaomusic.const import (
    PLAY_TYPE_ALL,
    PLAY_TYPE_ONE,
    PLAY_TYPE_RND,
    PLAY_TYPE_SEQ,
    PLAY_TYPE_SIN,
)
from xiaomusic.conversation import ConversationPoller
from xiaomusic.device_manager import DeviceManager
from xiaomusic.events import EventBus
from xiaomusic.music_library import MusicLibrary
from xiaomusic.utils.text_utils import chinese_to_number

from .dispatch import CommandDispatcher
from .now_playing import clean_display_name, resolve_current_guid
from .qrlogin import QRLogin


# 歌手合辑临时歌单名前缀（下划线开头：不会被用户口令/歌单模糊匹配命中）
SINGER_PLAYLIST_PREFIX = "__singer__"


class _AnalyticsStub:
    """XiaoMusicDevice._playmusic 会 `await analytics.send_play_event(...)`
    （device_player.py:612）——必须是协程，同步桩会在播放前一刻崩掉
    （2026-10-19 实测：TypeError: object NoneType can't be used in 'await'）。"""

    async def send_play_event(self, *args, **kwargs):
        pass


class NativeCore:
    """native 引擎：xiaomusic 组件装配 + 编排 + 命令入口。"""

    def __init__(self, cfg: Config, log: logging.Logger):
        self.config = cfg
        self.log = log
        self.event_bus = EventBus()
        self.analytics = _AnalyticsStub()
        # None → XiaoMusicDevice.auto_add_song 直接 return（不启用自动搜歌入歌单）
        self.js_plugin_manager = None
        self.running_task: list = []
        self.last_cmd = ""
        # 保序歌单名（桥接 push_config 下发）：顺序播放本地歌曲专用，
        # 内容与「本地收藏」相同但按收藏进入的先后排列、不乱序
        self.playlist_seq_name = ""
        # 需要「保持给定顺序」的歌单名（保序收藏 + 歌手合辑临时歌单）：
        # 上游入库/取下一曲时会按歌名重排，这些歌单的顺序必须原样保留
        self._order_guard_names: set = set()
        # 睡眠定时任务：did → asyncio.Task（到点 stop，重复口令先撤销旧的）
        self._sleep_timers: dict = {}
        # 口令收藏：桥接 /play 最后取流的 guid（音箱开播哪首就来取哪首的流）；
        # bridge 由 app.create_native 装配（State：fn 客户端 + favorites 快照）
        self.last_played_guid = ""
        self.last_played_title = ""
        self.last_played_at = 0.0
        self.bridge = None

        # 组件装配（顺序照搬 XiaoMusic.__init__ 中与本方案相关的部分）
        self.device_manager = DeviceManager(config=cfg, log=log, xiaomusic=self)
        self.auth_manager = AuthManager(
            config=cfg, log=log, device_manager=self.device_manager
        )
        self.music_library = MusicLibrary(
            config=cfg, log=log, event_bus=self.event_bus
        )
        # 必须在首次 gen_all_music_list 之前装：入库时上游就会排序
        self._install_seq_playlist_order_guard()
        self.music_library.gen_all_music_list()
        self.conversation_poller = ConversationPoller(
            config=cfg,
            log=log,
            auth_manager=self.auth_manager,
            device_manager=self.device_manager,
        )
        self.dispatcher = CommandDispatcher(self)
        self.qrlogin = QRLogin(cfg.conf_path)
        log.info("[native] 引擎装配完成（登录/设备/轮询/歌单/播放均复用上游组件）")

    # ---------------- 保序歌单保护（顺序播放本地歌曲） ----------------

    def _install_seq_playlist_order_guard(self):
        """让保序歌单躲开上游按歌名的字典序重排。

        上游有两处会把「非网络歌单」按歌名重排（都是 `sort(custom_sort_key)`）：
        1. MusicLibrary.gen_all_music_list 入库时（music_library.py:181）；
        2. XiaoMusicDevice.update_playlist 的顺序/循环分支（device_player.py:245），
           get_music（每一首下一曲）都会调它。
        两处的开关都是 `is_online_music(歌单名)`（上游只认 `_online_` 前缀）。
        于是「本地收藏（顺序）」推送时的收藏顺序，在入库那一刻就被歌名字典序
        覆盖——2026-10-28 实测：口令「顺序播放本地收藏」从「欢子 - 心痛2009」
        开始（该名字正是 282 首按 Unicode 码位排序后的第一位），而不是最新
        收藏的那首。收藏顺序丢失，顺序播放形同虚设。

        这里把保序歌单视为「网络歌单」以跳过这两处排序。安全性：上游对
        is_online_music 只有三处调用——上述两处，加 auto_add_song（native
        里 js_plugin_manager=None，永不执行）；取流走 is_web_music(name)
        （按 all_music 的 URL 判定），与歌单名无关，故不影响播放。
        """
        ml = self.music_library
        original = ml.is_online_music

        def is_online_music(cur_playlist):
            seq_name = self.playlist_seq_name
            if seq_name and cur_playlist == seq_name:
                return True
            if cur_playlist in self._order_guard_names:
                return True
            return original(cur_playlist)

        ml.is_online_music = is_online_music

    # ---------------- shim 方法（XiaoMusicDevice/DevicePlayer 会调用） ----------------

    async def do_tts(self, did, value):
        dev = self.device_manager.devices.get(did)
        if dev is None:
            return
        await dev.do_tts(value)

    async def handle_fatal_error(self, did, tts_msg="小music发生错误，请重试。"):
        """连续获取歌曲失败时的熔断播报（移植自 XiaoMusic.handle_fatal_error，
        native 不启用 edge-tts，统一走设备 TTS + 停止播放）。"""
        self.log.error("[native] handle_fatal_error did=%s: %s", did, tts_msg)
        try:
            await self.do_tts(did, tts_msg)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 错误播报失败: %s", e)
        dev = self.device_manager.devices.get(did)
        if dev is not None:
            try:
                await dev.stop()
            except Exception:  # noqa: BLE001
                pass

    async def add_singer_song(self, list_name, name):
        # 依赖 online_music_service（native 不启用）；auto_add_song 也被
        # js_plugin_manager=None 关闭，正常不会走到
        return None

    # ---------------- 命令入口（移植自 XiaoMusic，去掉用不到的分支） ----------------

    def _safe_list_name(self, list_name: str) -> str:
        """把口令里的歌单名解析成真实歌单名；解析不到返回空串（调用方负责提示）。

        上游 `find_real_music_list_name` 内部是 `find_best_match(...)[0]`，
        匹配不到时不返回空串而是直接 IndexError（2026-11-25 实测：重新配置后
        歌单名变成「收藏歌单」，说「播放本地收藏歌单」直接越界，异常冒到
        poller 把整个口令轮询打死，桥接从此听不到任何指令）。这里兜住：
        歌单表为空（首次同步未完成）或匹配失败都返回空串，由调用方 TTS 提示。
        """
        music_list = getattr(self.music_library, "music_list", None) or {}
        if not music_list:
            self.log.info("[native] 歌单表为空，跳过匹配 %r", list_name)
            return ""
        real = ""
        try:
            real = self.music_library.find_real_music_list_name(list_name)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 歌单名匹配失败 %r: %s", list_name, e)
        if real and real in music_list:
            return real
        self.log.info("[native] 歌单名 %r 未命中（现有 %d 个）", list_name, len(music_list))
        try:
            return self._fallback_collect_list(list_name)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 歌单回落失败 %r: %s", list_name, e)
            return ""

    def _fallback_collect_list(self, wanted: str) -> str:
        """口令名匹配不上时按语义回落：含「收藏」的口令 → 唯一的收藏歌单。

        实测场景（2026-11-25）：配置里歌单叫「收藏歌单」，用户喊「播放本地
        收藏歌单」，上游模糊匹配达不到阈值 → 越界。这里在**唯一**候选时回落，
        有多个含「收藏」的歌单（乱序 + 顺序两份）就放弃，交给调用方提示，
        宁可让用户换个说法也不要播错歌单。
        """
        if "收藏" not in (wanted or ""):
            return ""
        music_list = getattr(self.music_library, "music_list", None) or {}
        cands = [n for n in music_list if "收藏" in n and "顺序" not in n]
        if len(cands) == 1:
            self.log.info("[native] 歌单名 %r 回落为 %r", wanted, cands[0])
            return cands[0]
        return ""

    async def play_music_list(self, did="", arg1="", **kwargs):
        parts = (arg1 or "").split("|")
        wanted = parts[0]
        music_name = parts[1] if len(parts) > 1 else ""
        # 模糊匹配歌单名（空串返回第一个歌单，委托 music_library）
        list_name = self._safe_list_name(wanted)
        if not list_name:
            await self.do_tts(did, f"没有找到歌单{wanted}，请稍后再试" if wanted else "没有找到歌单")
            return
        await self.device_manager.devices[did].play_music_list(list_name, music_name)

    async def play_favorites_seq(self, did="", arg1="", **kwargs):
        """口令:顺序播放本地歌曲——按收藏进入歌单的先后，从第一首开始播。

        与「播放收藏」（乱序）的差别在两点，缺一不可：
        1. 歌单内容是桥接另推的保序歌单（同名两份：一份乱序一份保序）；
        2. 设备临时切「播放全部」模式（PLAY_TYPE_ALL，即整张循环）：
           上游 update_playlist 在随机模式下会 force_reshuffle，保序歌单
           也会被洗乱；ALL/SEQ 分支才按歌单原序取曲。
        为什么用 ALL 而不是 SEQ：SEQ 触底返回空串即停（get_music 里
        显式判断 "顺序播放结束"），而用户要的是**整张收藏放完再从头来**
        （2026-10-28 需求）；ALL 触底 new_index=0 回卷到第一首，循环。
        第一首显式点名播放（上游 _play_next 恒取"下一首"，空位置会跳过第一首）。
        之后的「播放收藏」由桥接 patch 负责把设备切回随机模式。
        歌单顺序由 _install_seq_playlist_order_guard 保住（上游默认会按
        歌名重排，收藏顺序在入库时就丢了）。
        """
        dev = self.device_manager.devices.get(did)
        if dev is None:
            self.log.warning("[native] 顺序播放：设备 %s 不在列表", did)
            return
        list_name = self.playlist_seq_name or "本地收藏（顺序）"
        if list_name not in self.music_library.music_list:
            # 歌单还没推下来（首次同步未完成）：明确播报，不静默失败
            await self.do_tts(did, "顺序歌单还没准备好，请稍后再试")
            return
        ordered = self.music_library.music_list[list_name]
        if not ordered:
            await self.do_tts(did, "顺序歌单是空的")
            return
        # 第一首必须显式点名播放：上游 _play_next→get_music 恒取"当前曲的
        # 下一首"（index+1），空位置会落到第二首——随机模式无感，顺序模式
        # 就是"跳过第一首收藏"，与需求直接相悖。
        first = ordered[0]
        try:
            # 整张循环 + 不播报模式切换（dotts=False）：与歌单播放保持一致安静
            await dev.set_play_type(PLAY_TYPE_ALL, False)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 整张循环模式切换失败: %s", e)
            return
        self.log.info("[native] 顺序播放歌单 %s（收藏顺序循环，从第一首 %r 起）", list_name, first)
        await dev.play_music_list(list_name, first)

    async def play_singer_favorites(self, did="", arg1="", **kwargs):
        """口令:播放X的本地收藏歌曲——收藏里该歌手的歌随机打乱成一张临时歌单。

        与点歌（play）的区别：点歌只播一首（收藏命中播那首、否则搜在线源下载）；
        这里是「这个歌手的全部收藏，随机顺序连着放」。

        顺序为什么能保住：临时歌单名登记进 _order_guard_names，guard 让上游
        is_online_music() 返回 True → 入库与 update_playlist 都不按歌名重排，
        否则我们打乱的顺序会被字典序抹平（和「顺序播放本地收藏」同一个坑）。
        播放模式用 ALL（整张循环）：这批歌放完回到第一首继续，适合当背景音。
        """
        singer = (arg1 or "").strip()
        dev = self.device_manager.devices.get(did)
        if dev is None:
            self.log.warning("[native] 歌手合辑：设备 %s 不在列表", did)
            return
        if not singer:
            await self.do_tts(did, "没听清要放哪位歌手的歌")
            return
        lib = self.music_library
        # 收藏全集：优先保序歌单（内容就是全部收藏），拿不到再退到第一个业务歌单
        source_name = self.playlist_seq_name
        if source_name not in lib.music_list:
            source_name = next(
                (n for n in lib.music_list if n not in ("全部", "所有歌曲")), ""
            )
        pool = lib.music_list.get(source_name) or []
        names = [n for n in pool if singer in (n or "")]
        if not names:
            self.log.info(
                "[native] 收藏里没有 %r 的歌（歌单 %s，共 %d 首）", singer, source_name, len(pool)
            )
            await self.do_tts(did, f"收藏里没有{singer}的歌")
            return
        random.shuffle(names)
        list_name = f"{SINGER_PLAYLIST_PREFIX}{singer}"
        lib.music_list[list_name] = names
        self._order_guard_names.add(list_name)
        try:
            # 整张循环 + 不播报模式切换（dotts=False）：口令播放保持安静
            await dev.set_play_type(PLAY_TYPE_ALL, False)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 歌手合辑模式切换失败: %s", e)
            return
        self.log.info(
            "[native] 歌手合辑 %s：%d 首随机播放（首曲 %r，歌单 %s）",
            singer, len(names), names[0], list_name,
        )
        await dev.play_music_list(list_name, names[0])

    async def play(self, did="", arg1="", **kwargs):
        parts = (arg1 or "").split("|")
        search_key = parts[0]
        name = parts[1] if len(parts) > 1 else search_key
        if not name:
            name = search_key
        await self.device_manager.devices[did].play(name, search_key)

    # ---------------- 口令轮询入口（ConversationPoller 回调） ----------------

    async def do_check_cmd(self, did="", query="", ctrl_panel=True, **kwargs):
        self.last_cmd = query
        try:
            await self.dispatcher.dispatch(did, query, ctrl_panel)
        except Exception as e:  # noqa: BLE001
            # 单条口令异常绝不能冒到 poller：那会让整个轮询任务退出，桥接
            # 从此收不到任何口令、管理台也不再更新"正在播放"（2026-11-25
            # 实测：歌单名匹配 IndexError 直接打死轮询）。只记日志，继续轮询。
            self.log.warning("[native] 口令处理异常 query=%r: %s", query, e, exc_info=True)

    async def reset_timer_when_answer(self, answer_length, did):
        dev = self.device_manager.devices.get(did)
        if dev is not None:
            await dev.reset_timer_when_answer(answer_length)

    # ---------------- 其余口令动作（移植自 XiaoMusic，均为薄委托） ----------------
    # 缺失任何一个的后果不是"没反应"而是"半截行为"：分发器执行口令前都会
    # group_force_stop_xiaoai 暂停音箱，但动作方法不存在时 is_playing 仍为
    # True、下一曲定时器不取消—— poller 的 reset_timer_when_answer 会把定时器
    # 重新 armed，到点自动播下一首（2026-10-19 实测：停止播放后 58 秒自行续播）。

    async def play_next(self, did="", **kwargs):
        return await self.device_manager.devices[did].play_next()

    async def play_prev(self, did="", **kwargs):
        return await self.device_manager.devices[did].play_prev()

    async def stop(self, did="", arg1="", **kwargs):
        # 设备侧 stop：is_playing=False + 取消组内下一曲定时器 + 停音箱（+TTS）
        return await self.device_manager.devices[did].stop(arg1=arg1)

    async def playlocal(self, did="", arg1="", **kwargs):
        return await self.device_manager.devices[did].playlocal(arg1)

    async def set_play_type(self, did="", play_type=PLAY_TYPE_RND, dotts=True):
        await self.device_manager.devices[did].set_play_type(play_type, dotts)

    async def set_play_type_one(self, did="", **kwargs):
        await self.set_play_type(did, PLAY_TYPE_ONE)

    async def set_play_type_all(self, did="", **kwargs):
        await self.set_play_type(did, PLAY_TYPE_ALL)

    async def set_play_type_rnd(self, did="", **kwargs):
        await self.set_play_type(did, PLAY_TYPE_RND)

    async def set_play_type_sin(self, did="", **kwargs):
        await self.set_play_type(did, PLAY_TYPE_SIN)

    async def set_play_type_seq(self, did="", **kwargs):
        await self.set_play_type(did, PLAY_TYPE_SEQ)

    async def stop_after_minute(self, did="", arg1=0, **kwargs):
        try:
            minute = int(arg1)
        except (KeyError, ValueError):
            minute = chinese_to_number(str(arg1))
        return await self.device_manager.devices[did].stop_after_minute(minute)

    async def sleep_timer(self, did="", arg1=None, **kwargs):
        """口令:半小时后停止播放/定时30分钟——到点自动停止播放。

        与上游 stop_after_minute（N分钟后关机）并存：这里是 native 侧实现，
        支持「半小时」等中文时长；重复口令先撤销旧定时。用户中途点歌不取消
        （睡前听歌场景，定时是绝对到期）；只有「取消定时」口令撤销。
        """
        try:
            minutes = int(arg1)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            minutes = 30
        old = self._sleep_timers.pop(did, None)
        if old is not None:
            old.cancel()

        async def _do_stop():
            await asyncio.sleep(minutes * 60)
            self._sleep_timers.pop(did, None)
            self.log.info("[native] 睡眠定时到点，停止播放 did=%s", did)
            try:
                await self.stop(did=did, arg1="notts")
            except Exception as e:  # noqa: BLE001
                self.log.warning("[native] 睡眠定时停止失败: %s", e)

        task = asyncio.create_task(_do_stop())
        self._sleep_timers[did] = task
        if minutes >= 60:
            desc = f"{minutes // 60}小时" if minutes % 60 == 0 else f"{minutes}分钟"
        else:
            desc = f"{minutes}分钟"
        self.log.info("[native] 睡眠定时 %d 分钟 did=%s", minutes, did)
        await self.do_tts(did, f"好，{desc}后停止播放")

    async def cancel_sleep_timer(self, did="", arg1="", **kwargs):
        """口令:取消定时——撤销未触发的睡眠定时（顺带撤上游的定时关机）。"""
        task = self._sleep_timers.pop(did, None)
        dev = self.device_manager.devices.get(did)
        upstream = getattr(dev, "_stop_timer", None)
        if upstream is not None:
            upstream.cancel()
            dev._stop_timer = None
        if task is not None or upstream is not None:
            self.log.info("[native] 已取消睡眠定时 did=%s", did)
            await self.do_tts(did, "已取消睡眠定时")
        else:
            await self.do_tts(did, "当前没有睡眠定时")

    def current_play_guid(self, did: str = "") -> tuple[str, str]:
        """当前播放曲目的 (guid, 歌名)：设备上报曲目反查桥接 URL 优先。

        不能只信 last_played_guid：上游「随机列表播完一轮」时会在后台预缓存
        下一首，预缓存也请求桥接 /play 取流，把 last_played 覆盖成下一首
        （2026-11-23 实测：点《就是爱你》30 秒后「收藏这首歌」收藏到
        《分手季节》）。解析逻辑见 native/now_playing.py。
        """
        lib = getattr(self, "music_library", None)
        dm = getattr(self, "device_manager", None)
        return resolve_current_guid(
            getattr(dm, "devices", {}) or {},
            (getattr(lib, "all_music", {}) or {}) if lib is not None else {},
            getattr(self, "last_played_guid", ""),
            getattr(self, "last_played_title", ""),
            did,
        )

    async def fav_current(self, did="", arg1="", **kwargs):
        """口令:收藏这首歌——收藏当前播放歌曲到飞牛音乐账号。

        guid 取「设备上报当前曲目」（current_play_guid），不用 last_played_guid：
        后者会被上游的下一首预缓存请求污染。官方 guid 直接建官方红心；在线
        guid 走代理的红心自动下载链路（整轨落库→官方收录→转官方红心，音质
        门槛自动生效）。已在收藏中则不重复写。"""
        guid, title = self.current_play_guid(did)
        if not guid:
            await self.do_tts(did, "没有正在播放的歌曲，先点首歌吧")
            return
        bridge = getattr(self, "bridge", None)
        fn = getattr(bridge, "fn", None) if bridge is not None else None
        if fn is None:
            self.log.warning("[native] 收藏当前歌曲失败：桥接客户端未就绪")
            await self.do_tts(did, "收藏服务还没就绪，稍后再试")
            return
        # 展示名去掉音源标记（「[kuwo]就是爱你」→「就是爱你」），避免 TTS 念英文
        display = clean_display_name(title)
        label = f"《{display}》" if display else "这首歌"
        # 不再用"已收藏"提前返回：官方 isFavorite 与 App 显示可能不一致
        # （2026-11-23 实测：接口列表明确 isFavorite=true、App 却显示未收藏，
        # 用户被"已经在收藏里了"卡住，无法完成收藏这个动作本身）。实测
        # add_favorite 幂等——已收藏再调返回成功、收藏数不变，所以统一执行
        # 收藏再回查，答复与用户的操作意图一致。
        try:
            await fn.add_favorite(guid)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 收藏当前歌曲失败 guid=%s: %s", guid[:24], e)
            await self.do_tts(did, f"收藏{label}失败了，请稍后再试")
            return
        self.log.info("[native] 收藏接口已受理 guid=%s，回查确认", guid[:24])
        if guid.startswith("online:"):
            # 在线 guid：走代理红心链路异步下载转正，无法立即回查
            await self.do_tts(did, f"好的，正在收藏{label}，下载完成后自动加入收藏")
            return
        # 官方 guid：回查收藏列表确认真的落库。接口 code=0 不等于一定可见
        # （2026-11-23 用户反馈：提示已收藏，但 App 里看不到）。确认后才敢
        # 说"已收藏"，并直接用官方列表刷新本地快照——绝不伪造条目，
        # 否则下一句口令会误判"已在收藏中"而永不重试。
        if await self._confirm_favorite(fn, bridge, guid):
            await self.do_tts(did, f"好的，已收藏{label}")
        else:
            self.log.warning("[native] 收藏回查未命中 guid=%s", guid[:24])
            await self.do_tts(did, f"收藏请求已提交，收藏列表还没同步出来，稍后刷新看看")

    async def _live_favorite_guids(self, fn, bridge):
        """实时拉官方收藏 guid 集合并刷新本地快照；失败返回 None（调用方退回快照）。"""
        try:
            favs = await fn.get_favorites()
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 收藏列表实时查询失败: %s", e)
            return None
        try:
            bridge.favorites = favs
        except Exception:  # noqa: BLE001
            pass
        return {str(t.get("guid") or "") for t in favs}

    async def _confirm_favorite(self, fn, bridge, guid: str) -> bool:
        """回查官方收藏列表确认落库（带一次短暂重试，官方索引可能有延迟）。"""
        for attempt in (1, 2):
            live = await self._live_favorite_guids(fn, bridge)
            if live and guid in live:
                self.log.info("[native] 收藏回查命中 guid=%s（第 %d 次）", guid[:24], attempt)
                return True
            if attempt == 1:
                await asyncio.sleep(0.8)
        self.log.warning("[native] 收藏回查未命中 guid=%s（官方列表里没有）", guid[:24])
        return False

    async def lyric_search_unsupported(self, did="", arg1="", **kwargs):
        """口令「搜歌词 xxx」：音箱端不支持歌词搜索，明确提示，不触发任何搜索。

        歌词搜索只在 App/管理台搜索框可用（网关按「搜歌词」前缀处理）；
        音箱这边如果放进去，整串会被当歌名搜，什么都搜不到，还会误触发
        网关的歌词搜索。直接拒掉更清楚（2026-11-23 用户需求）。
        """
        await self.do_tts(did, "小爱音箱不支持按歌词搜歌，请直接说歌名")

    async def play_music_list_index(self, did="", arg1="", **kwargs):
        """口令:播放列表第几个xx（移植自 XiaoMusic.play_music_list_index）"""
        patternarg = r"^([零一二三四五六七八九十百千万亿]+)个(.*)"
        matcharg = re.match(patternarg, arg1)
        if not matcharg:
            return await self.play_music_list(did, arg1)

        chinese_index, list_name = matcharg.groups()
        real_name = self._safe_list_name(list_name)
        if not real_name:
            await self.do_tts(did, f"没有找到歌单{list_name}，请稍后再试")
            return
        list_name = real_name

        index = chinese_to_number(chinese_index)
        play_list = self.music_library.music_list[list_name]
        if 0 <= index - 1 < len(play_list):
            music_name = play_list[index - 1]
            self.log.info(f"即将播放 {arg1} 里的第 {index} 个: {music_name}")
            await self.device_manager.devices[did].play_music_list(
                list_name, music_name
            )
            return
        await self.do_tts(did, f"播放列表{list_name}中找不到第{index}个")

    async def select_index(self, did="", arg1="", **kwargs):
        """口令:第几个（多匹配待选态的回复，移植自 XiaoMusic.select_index）"""
        patternarg = r"^第?([零一二三四五六七八九十百千万亿]+)[个首条集]$"
        matcharg = re.match(patternarg, arg1)
        if not matcharg:
            return
        index = chinese_to_number(matcharg.groups()[0])
        device = self.device_manager.devices.get(did)
        if not device:
            self.log.warning(f"设备 did:{did} 不存在")
            return
        await device.handle_selection(index)

    async def gen_music_list(self, **kwargs):
        """口令:刷新列表"""
        self.music_library.gen_all_music_list()
        self.update_all_playlist()
        self.log.info("gen_music_list ok")

    def update_all_playlist(self):
        """更新每个设备的歌单（设备侧 update_playlist 会按需洗牌）。"""
        for device in self.device_manager.devices.values():
            device.update_playlist()

    # ---------------- 生命周期 ----------------

    async def run_forever(self):
        """主循环（照搬 XiaoMusic.run_forever）+ 轮询守护。

        轮询是桥接的命根子：`run_conversation_loop` 一旦异常退出，口令就再也
        收不到、管理台「正在播放」也不再更新，只能重启进程（2026-11-25 实测：
        一句歌单口令的 IndexError 就把轮询打死了）。这里做成守护循环——
        异常或意外退出后按退避重建，连续正常运行超过 60 秒则重置退避。
        """
        self.log.info("[native] run_forever start")
        self.music_library.try_gen_all_music_tag()
        try:
            await self.auth_manager.init_all_data()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # 登录失败不阻断轮询（轮询内部按登录态自行退避），只记录
            self.log.error("[native] 初始化登录失败: %s", e, exc_info=True)

        backoff = 2
        while True:
            started = time.monotonic()
            try:
                await self.conversation_poller.run_conversation_loop(
                    self.do_check_cmd, self.reset_timer_when_answer
                )
                self.log.warning("[native] 口令轮询循环退出，%d 秒后重建", backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log.error("[native] 口令轮询异常退出: %s，%d 秒后重建",
                               e, backoff, exc_info=True)
            if time.monotonic() - started > 60:
                backoff = 2  # 跑稳过一阵再崩的，从最小退避重来
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def cancel_all_tasks(self):
        for t in list(self.running_task):
            t.cancel()
        self.running_task.clear()
        for t in list(self._sleep_timers.values()):
            t.cancel()
        self._sleep_timers.clear()
        # AuthManager.__init__ 建了 mi_session（aiohttp），关闭时释放
        session = getattr(self.auth_manager, "mi_session", None)
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 桥接调用面 ----------------

    def mi_logged_in(self) -> bool:
        """小米登录是否真实有效（mina_service 存在说明登录成功）。"""
        return self.auth_manager is not None and self.auth_manager.mina_service is not None

    def push_config(
        self,
        *,
        music_list_json: str,
        keywords_playlist: str,
        keywords_playlocal: str,
        user_key_word_dict: dict,
        music_list_url: str,
        playlist_seq_name: str = "",
    ):
        """桥接推送歌单/口令配置（native 模式下替代 /xm/api/system/modifiysetting）。

        歌单走 music_list_json（MusicLibrary._append_music_list 消费它，
        与 xiaomusic 引擎 modifiysetting 推送同一字段）；口令变体经
        Config.update_config 内部 init() 重建 key_match_order/key_word_dict。
        playlist_seq_name 是保序歌单名（顺序播放本地歌曲专用）。
        """
        self.playlist_seq_name = playlist_seq_name or ""
        self.config.update_config({
            "music_list_json": music_list_json,
            "keywords_playlist": keywords_playlist,
            "keywords_playlocal": keywords_playlocal,
            "user_key_word_dict": dict(user_key_word_dict),
            "music_list_url": music_list_url,
        })
        self.music_library.gen_all_music_list()

    async def refresh_music_list(self):
        """本地目录变化后重载歌单（native 模式下替代 /xm/api/music/refreshlist
        的 gen_all_music_list 部分；music_list_json 已在内存，无需重拉 URL）。"""
        self.music_library.gen_all_music_list()

    async def fetch_devices(self, trigger_login: bool = False) -> list:
        """拉取设备列表（含 did 映射），trigger_login=True 先强制重新登录。

        注意：device_manager.devices 会按 mi_did 过滤（AuthManager.
        try_update_device_id 只放行选中项），已选过设备时只含选中项；
        向导勾选需要全量列表，故刷新设备管理器后，再直接从 mina_service
        取原始设备表（与 xiaomusic 引擎 /xm/getsetting 的 device_list 等价）。
        """
        if trigger_login:
            await self.auth_manager.init_all_data(force_login=True)
        # 刷新设备管理器（按当前 mi_did 过滤后的生效设备）
        await self.device_manager.update_device_info(self.auth_manager)
        # 全量设备（未过滤）——向导勾选用
        raw = []
        mina = getattr(self.auth_manager, "mina_service", None)
        if mina is not None:
            try:
                raw = await mina.device_list() or []
            except Exception as e:  # noqa: BLE001
                self.log.warning("[native] device_list 失败: %s", e)
        devices = []
        for h in raw:
            device_id = h.get("deviceID", "")
            if not device_id:
                continue
            devices.append({
                "deviceID": device_id,
                "name": h.get("alias") or h.get("name", ""),
                "hardware": h.get("hardware", ""),
                "did": h.get("miotDID", ""),
            })
        return devices
