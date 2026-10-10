#!/usr/bin/env python3
"""调试前端用：起一个带假机器人状态的日历页面。

`main.py all` 要真登录微信才看得到「未登录 + 二维码」这块界面，
调样式的时候没必要真去扫码。这个脚本塞一个假的 ClawBot 状态进去。

    python tools/preview_web.py              # 未登录，页面显示二维码
    python tools/preview_web.py --logged-in  # 已登录
    python tools/preview_web.py --off        # 完全模拟「只跑网页，机器人没开」
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod  # noqa: E402
from app import db, timeutil as tu  # noqa: E402
from app import web as webmod  # noqa: E402


def _placeholder_png(modules: int = 21, scale: int = 10) -> bytes:
    """现画一张「一眼看出是二维码」的占位图，免得预览时那块是个碎图。

    真的二维码长什么样不重要，重要的是那个框的尺寸、留白和对比度对不对。
    """
    size = modules * scale
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # 每行的 filter type
        my = y // scale
        for x in range(size):
            mx = x // scale
            finder = (
                (mx < 7 and my < 7)
                or (mx >= modules - 7 and my < 7)
                or (mx < 7 and my >= modules - 7)
            )
            if finder:
                dx = min(mx % 7, 6 - mx % 7)
                dy = min(my % 7, 6 - my % 7)
                on = max(dx, dy) != 1  # 7x7 定位角：外框黑、内圈白、中心 3x3 黑
            else:
                on = (mx * 7 + my * 13 + mx * my * 3) % 3 == 0
            value = 0 if on else 255
            raw += bytes((value, value, value))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8bit truecolor
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# 1x1 的透明 PNG 太看不见了，这里现造一张能看出「这是一张二维码图」的占位图。
_PNG = _placeholder_png()


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，print 里的 emoji 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--logged-in", action="store_true", help="假装已经登录")
    ap.add_argument("--off", action="store_true", help="假装机器人没在跑")
    args = ap.parse_args(argv)

    cfgmod.ensure_config_file(Path(args.config) if args.config else None)
    cfg = cfgmod.load(Path(args.config) if args.config else None)
    tu.configure(cfg.get("timezone") or "Asia/Shanghai")
    db.init(cfg.path(cfg.get("db_path") or "data/woolbot.db"))

    qr_path = None
    if not (args.logged_in or args.off):
        out = Path(tempfile.gettempdir()) / "woolbot_preview_qr.png"
        try:
            out.write_bytes(_PNG)
            qr_path = str(out)
        except OSError:
            qr_path = None

    if args.off:
        webmod.set_status_provider(None)
    else:
        webmod.set_status_provider(lambda: {
            "bot": "on",
            "logged_in": args.logged_in,
            "ready": args.logged_in,
            "qr_path": qr_path,
            "qr_token": 1,
        })

    httpd = webmod.create_server(cfg.as_dict())
    host = (cfg.get("web") or {}).get("host") or "127.0.0.1"
    token = (cfg.get("web") or {}).get("token") or ""
    url = f"http://{host}:{httpd.server_address[1]}/" + (f"?token={token}" if token else "")
    print(f"🔍 预览地址：{url}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
