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
import re
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
from .qrlogin import QRLogin


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

    async def play_music_list(self, did="", arg1="", **kwargs):
        parts = (arg1 or "").split("|")
        list_name = parts[0]
        music_name = parts[1] if len(parts) > 1 else ""
        # 模糊匹配歌单名（空串返回第一个歌单，委托 music_library）
        list_name = self.music_library.find_real_music_list_name(list_name)
        if list_name not in self.music_library.music_list:
            await self.do_tts(did, f"播放列表{list_name}不存在")
            return
        await self.device_manager.devices[did].play_music_list(list_name, music_name)

    async def play_favorites_seq(self, did="", arg1="", **kwargs):
        """口令:顺序播放本地歌曲——按收藏进入歌单的先后，从第一首开始播。

        与「播放收藏」（乱序）的差别在两点，缺一不可：
        1. 歌单内容是桥接另推的保序歌单（同名两份：一份乱序一份保序）；
        2. 设备临时切顺序模式（PLAY_TYPE_SEQ）：上游 update_playlist 在随机
           模式下会 force_reshuffle，保序歌单也会被洗乱；顺序模式下
           _play_list 直接取歌单原序，且播完触底即停（不循环）。
        第一首显式点名播放（上游 _play_next 恒取"下一首"，空位置会跳过第一首）。
        之后的「播放收藏」由桥接 patch 负责把设备切回随机模式。
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
            # 顺序模式 + 不播报模式切换（dotts=False）：与歌单播放保持一致安静
            await dev.set_play_type(PLAY_TYPE_SEQ, False)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 顺序模式切换失败: %s", e)
            return
        self.log.info("[native] 顺序播放歌单 %s（收藏顺序，从第一首 %r 起）", list_name, first)
        await dev.play_music_list(list_name, first)

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
        await self.dispatcher.dispatch(did, query, ctrl_panel)

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

    async def fav_current(self, did="", arg1="", **kwargs):
        """口令:收藏这首歌——收藏当前播放歌曲到飞牛音乐账号。

        guid 来自桥接 /play（或点歌在线下载）记录的"最后取流"。官方 guid
        直接建官方红心；在线 guid 走代理的红心自动下载链路（整轨落库→官方
        收录→转官方红心，音质门槛自动生效）。已在收藏中则不重复写。"""
        guid = str(getattr(self, "last_played_guid", "") or "")
        title = str(getattr(self, "last_played_title", "") or "")
        if not guid:
            await self.do_tts(did, "没有正在播放的歌曲，先点首歌吧")
            return
        bridge = getattr(self, "bridge", None)
        fn = getattr(bridge, "fn", None) if bridge is not None else None
        if fn is None:
            self.log.warning("[native] 收藏当前歌曲失败：桥接客户端未就绪")
            await self.do_tts(did, "收藏服务还没就绪，稍后再试")
            return
        label = f"《{title}》" if title else "这首歌"
        # 已收藏判断：官方 guid 才有精确快照；在线 guid 无快照，直接走收藏
        # →代理自动下载转正（重复收藏由代理的绑定/去重机制兜底）
        favs = {
            str(t.get("guid") or "")
            for t in (getattr(bridge, "favorites", None) or [])
            if isinstance(t, dict)
        }
        if guid in favs:
            self.log.info("[native] 收藏口令：%s 已在收藏中", guid[:24])
            await self.do_tts(did, f"{label}已经在收藏里啦")
            return
        try:
            await fn.add_favorite(guid)
        except Exception as e:  # noqa: BLE001
            self.log.warning("[native] 收藏当前歌曲失败 guid=%s: %s", guid[:24], e)
            await self.do_tts(did, f"收藏{label}失败了，请稍后再试")
            return
        self.log.info("[native] 收藏当前歌曲成功 guid=%s", guid[:24])
        # 快照即时补一条：紧跟的重复口令不误判；官方同步循环稍后全量校正
        try:
            bridge.favorites = list(bridge.favorites or []) + [{"guid": guid}]
        except Exception:  # noqa: BLE001
            pass
        if guid.startswith("online:"):
            await self.do_tts(did, f"好的，正在收藏{label}，下载完成后自动加入收藏")
        else:
            await self.do_tts(did, f"好的，已收藏{label}")

    async def play_music_list_index(self, did="", arg1="", **kwargs):
        """口令:播放列表第几个xx（移植自 XiaoMusic.play_music_list_index）"""
        patternarg = r"^([零一二三四五六七八九十百千万亿]+)个(.*)"
        matcharg = re.match(patternarg, arg1)
        if not matcharg:
            return await self.play_music_list(did, arg1)

        chinese_index, list_name = matcharg.groups()
        list_name = self.music_library.find_real_music_list_name(list_name)
        if list_name not in self.music_library.music_list:
            await self.do_tts(did, f"播放列表{list_name}不存在")
            return

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
        """主循环（照搬 XiaoMusic.run_forever 的相关部分）。"""
        self.log.info("[native] run_forever start")
        self.music_library.try_gen_all_music_tag()
        await self.auth_manager.init_all_data()
        await self.conversation_poller.run_conversation_loop(
            self.do_check_cmd, self.reset_timer_when_answer
        )

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
