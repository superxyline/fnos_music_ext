"""扫码登录（复用 xiaomusic.qrcode_login.MiJiaAPI）。

流程与 xiaomusic api 的 /api/get_qrcode 完全一致：
1. get_qrcode() 拿二维码（已登录时返回 False）
2. 后台线程 get_logint_status(lp) 长轮询，扫码成功后把 passToken/userId
   等写入 conf/auth.json
3. AuthManager.init_all_data 读到 auth.json 完成登录（无需重启进程）
"""
import asyncio
import base64
import io


class QRLogin:
    def __init__(self, conf_dir):
        from xiaomusic.qrcode_login import MiJiaAPI

        self._api = MiJiaAPI(auth_data_path=str(conf_dir))

    def get_qrcode(self):
        """None/False=已登录无需扫码；否则返回含 qr/loginUrl/lp 的 dict。"""
        return self._api.get_qrcode()

    @staticmethod
    def image_url(qr_data: dict) -> str:
        """二维码图片 URL：优先小米官方 qr 字段；缺失时用 loginUrl 本地生成
        PNG data URI（镜像内 Pillow 由 musicbox 依赖提供）。"""
        qr = (qr_data or {}).get("qr") or ""
        if qr:
            return qr
        login_url = (qr_data or {}).get("loginUrl") or ""
        if not login_url:
            return ""
        import qrcode
        from PIL import Image

        img = qrcode.make(login_url)
        if not isinstance(img, Image.Image):
            img = img.get_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"

    def start_status_poll(self, lp: str):
        """后台轮询扫码状态（不阻塞响应）。扫码成功后 auth.json 落盘。

        超时（默认 120s）或异常时静默结束：用户重新生成二维码即可。
        """
        async def _poll():
            try:
                await asyncio.to_thread(self._api.get_logint_status, lp)
            except Exception:  # noqa: BLE001
                pass

        return asyncio.create_task(_poll())
