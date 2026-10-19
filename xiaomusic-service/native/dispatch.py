"""native 命令分发（移植自 xiaomusic.command_handler，只服务桥接配置的关键词）。

匹配策略与上游 do_check_cmd/match_cmd 一致：
1. 完全匹配：query 恰为关键词 → 对应动作
2. 模糊匹配：query 含关键词 → 取关键词后的文本为参数（个别关键词取前）
3. 激活口令过滤：未在播放且配置了激活口令时，只放行激活口令
4. 执行前先停小爱自带技能应答，避免"不支持"提示音抢占
"""
import re

from xiaomusic.config import KEY_WORD_ARG_BEFORE_DICT


class CommandDispatcher:
    def __init__(self, core):
        self.core = core
        self.config = core.config
        self.log = core.log

    async def dispatch(self, did, query, ctrl_panel=True):
        device = self.core.device_manager.devices.get(did)
        if device is None:
            self.log.warning("[native] 设备 %s 不在列表，忽略口令 %r", did, query)
            return
        opvalue, oparg = self.match_cmd(device, query, ctrl_panel)
        if not opvalue:
            self.log.info("[native] 未匹配到指令: %r", query)
            return
        # 执行前先停小爱，避免播放"不支持"提示
        try:
            await device.group_force_stop_xiaoai()
        except Exception as e:  # noqa: BLE001
            self.log.debug("[native] group_force_stop_xiaoai 失败: %s", e)
        func = getattr(self.core, opvalue, None)
        if func is None:
            self.log.warning("[native] 指令 %s 在 native 引擎无对应实现", opvalue)
            return
        self.log.info(
            "[native] 执行指令 %s arg=%r did=%s", opvalue, oparg, did
        )
        await func(did=did, arg1=oparg)

    def match_cmd(self, device, query, ctrl_panel):
        """返回 (动作名, 参数)，未匹配返回 (None, None)。"""
        # 1) 完全匹配
        opvalue = self.check_full_match_cmd(device, query, ctrl_panel)
        if opvalue:
            return opvalue, ""
        # 2) 按优先级模糊匹配
        for opkey in self.config.key_match_order:
            matcharg = re.match(rf"(.*){opkey}(.*)", query)
            if not matcharg:
                continue
            argpre, argafter = matcharg.groups()
            oparg = argafter
            if opkey in KEY_WORD_ARG_BEFORE_DICT:
                oparg = argpre
            opvalue = self.config.key_word_dict.get(opkey)
            if not opvalue:
                continue
            active_cmd_arr = self.config.get_active_cmd_arr()
            if (
                not ctrl_panel
                and not device.is_playing
                and active_cmd_arr
                and opvalue not in active_cmd_arr
                and opkey not in active_cmd_arr
            ):
                continue
            return opvalue, oparg
        return None, None

    def check_full_match_cmd(self, device, query, ctrl_panel):
        if query not in self.config.key_match_order:
            return None
        active_cmd_arr = self.config.get_active_cmd_arr()
        opvalue = self.config.key_word_dict.get(query)
        if (
            ctrl_panel
            or device.is_playing
            or not active_cmd_arr
            or opvalue in active_cmd_arr
        ):
            return opvalue
        return None
