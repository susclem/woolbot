"""把一段文本（通常是登录链接）画成二维码 PNG。

为什么要自己画：协议里的 `qrcode_img_content` 是一个 HTTPS 链接
（`https://liteapp.weixin.qq.com/q/...`），不是图片。那个链接指向一个
**JS 渲染的网页**，直接下载只会拿到一坨 HTML —— 上游文档说「优先下载二维码
图片」，实测这条路走不通。上游自己的兜底就是拿这个 URL 当载荷本地生成二维码
（见 docs/reference/bot.py 的 `render_generated_qr`），这里照做。

用 segno 而不是 qrcode+pillow：segno 是纯 Python、零依赖，能直接写 PNG
（自带 zlib 编码器），slim 镜像里不用再塞一个 Pillow。没有 segno 时
`render_png` 返回 None，调用方退回「只显示链接」，功能不中断。
"""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

try:  # segno 是可选依赖
    import segno as _segno
except ImportError:  # pragma: no cover - 取决于环境
    _segno = None


def available() -> bool:
    return _segno is not None


def render_png(text: str, scale: int = 6, border: int = 3) -> Optional[bytes]:
    """把 text 编成二维码 PNG。没装 segno 或内容非法时返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if _segno is None:
        log.warning("没装 segno，无法生成二维码图片；只显示链接。装法：pip install segno")
        return None
    try:
        buf = io.BytesIO()
        # 用 M 级纠错：扫码场景下容错够用，码也不会太密
        _segno.make(text, error="m").save(buf, kind="png", scale=scale, border=border)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - 画不出来不该影响登录
        log.warning("生成二维码失败：%s", exc)
        return None
