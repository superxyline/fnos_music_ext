"""native 引擎：复用 xiaomusic 上游的登录/设备/轮询/播放组件，自建编排与分发。

与 xiaomusic 引擎（XiaoMusic 主类 + api 子应用）并行为双引擎之一，
由桥接配置 engine 字段选择（xiaomusic | native）。
"""
