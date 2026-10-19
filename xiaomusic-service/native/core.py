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
import logging
from pathlib import Path

from xiaomusic.auth import AuthManager
from xiaomusic.config import Config
from xiaomusic.conversation import ConversationPoller
from xiaomusic.device_manager import DeviceManager
from xiaomusic.events import EventBus
from xiaomusic.music_library import MusicLibrary

from .dispatch import CommandDispatcher
from .qrlogin import QRLogin


class _AnalyticsStub:
    """XiaoMusicDevice._play_internal 会调 analytics.send_play_event。"""

    def send_play_event(self, *args, **kwargs):
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

    def update_keywords(
        self,
        *,
        keywords_playlist: str,
        keywords_playlocal: str,
        user_key_word_dict: dict,
        music_list_url: str,
    ):
        """桥接推送歌单/口令变体（native 模式下替代 /xm/api/system/modifiysetting）。

        Config.update_config 内部会调 init() 重建 key_match_order/key_word_dict。
        """
        self.config.update_config({
            "keywords_playlist": keywords_playlist,
            "keywords_playlocal": keywords_playlocal,
            "user_key_word_dict": dict(user_key_word_dict),
            "music_list_url": music_list_url,
        })

    async def refresh_music_list(self):
        """歌单/本地目录变化后重载（native 模式下替代 /xm/api/music/refreshlist）。"""
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
