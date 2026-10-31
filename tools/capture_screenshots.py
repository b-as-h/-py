#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用固定虚拟数据生成 README 展示截图。

不读取真实串口、本机性能或 HWiNFO，四张图可稳定复现：
    screenshot.png                 主界面
    screenshots/trends.png         温度曲线 + 3D 姿态仪
    screenshots/radar.png          雷达扫描
    screenshots/gyro.png           3D 姿态仪特写

用法（Windows，屏幕工作区至少 1400x1020）：
    python tools/capture_screenshots.py
"""

import ctypes
import math
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
import host_monitor as hm  # noqa: E402


class DemoCollector:
    """固定性能快照，替代 sysinfo.Collector。"""

    def __init__(self, interval=1.0):
        self.interval = interval

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {
            "cpu": 34.0, "mem": 62.0, "gpu": 27.0,
            "cpu_temp": 54.0, "gpu_temp": 48.0,
            "cpu_mhz": 4380.0, "gpu_mhz": 1860.0,
            "ram_used": 19.8, "ram_total": 31.9,
            "net_down": 126.4, "net_up": 18.2,
            "fps": 144.0, "hz": 144.0,
        }


class FakeSerial:
    def write(self, _data):
        return len(_data)

    def close(self):
        pass


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("width", wintypes.LONG),
        ("height", wintypes.LONG), ("planes", wintypes.WORD),
        ("bit_count", wintypes.WORD), ("compression", wintypes.DWORD),
        ("size_image", wintypes.DWORD), ("x_pels", wintypes.LONG),
        ("y_pels", wintypes.LONG), ("clr_used", wintypes.DWORD),
        ("clr_important", wintypes.DWORD),
    ]


def _png_chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def save_png(path, width, height, bgra):
    """把 Win32 GetDIBits 的 32 位像素数据编码为 PNG。"""
    rows = bytearray()
    for y in range(height - 1, -1, -1):  # DIB 正高度是自下而上
        rows.append(0)  # filter: none
        start = y * width * 4
        for x in range(width):
            p = start + x * 4
            rows.extend((bgra[p + 2], bgra[p + 1], bgra[p]))
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def capture_client(root, path):
    """截取 Tk 客户区，不包含 Windows 标题栏。"""
    root.update_idletasks()
    root.update()
    time.sleep(0.12)
    root.update()
    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
    _capture_handle(hwnd, path)


def capture_widget(widget, path):
    """直接截取任意 Tk 控件的屏幕区域，用于姿态仪特写。"""
    widget.update_idletasks()
    widget.update()
    time.sleep(0.12)
    widget.update()
    x = widget.winfo_rootx()
    y = widget.winfo_rooty()
    width = widget.winfo_width()
    height = widget.winfo_height()

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    gdi32.BitBlt(mem_dc, 0, 0, width, height, hdc, x, y, 0x00CC0020)
    _save_bitmap(mem_dc, bitmap, width, height, path)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, hdc)


def _save_bitmap(mem_dc, bitmap, width, height, path):
    gdi32 = ctypes.windll.gdi32
    info = _BitmapInfoHeader()
    info.size = ctypes.sizeof(_BitmapInfoHeader)
    info.width = width
    info.height = height
    info.planes = 1
    info.bit_count = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
    save_png(path, width, height, buffer.raw)
    print(f"saved {path.relative_to(ROOT)} ({width}x{height})")


def _capture_handle(hwnd, path):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right, rect.bottom

    hdc = user32.GetDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    gdi32.BitBlt(mem_dc, 0, 0, width, height, hdc, 0, 0, 0x00CC0020)
    _save_bitmap(mem_dc, bitmap, width, height, path)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hdc)


def scroll_to(app, widget, margin=12):
    """将左栏滚动到指定区块顶部。"""
    app.scroll.update_idletasks()
    top = (widget.winfo_rooty() - app.scroll.winfo_rooty()
           + app.scroll.canvasy(0) - hm._sp(margin))
    total = max(1, app.scroll.bbox("all")[3])
    app.scroll.yview_moveto(max(0.0, top / total))
    app.root.update_idletasks()
    app.root.update()


def populate(app):
    app.ser = FakeSerial()
    app.active_baud = 115200
    app.await_pong = 0.0
    app.gyro_link = True
    app.port_box["values"] = ("COM5  HC-05 (SPP)",)
    app.port_var.set("COM5  HC-05 (SPP)")
    app.baud_var.set("115200")
    app.connect_btn.configure(text="断开连接")
    app._set_status("在线 @ 115200", "ok")
    if getattr(app, "_toast_job", None):
        app.root.after_cancel(app._toast_job)
        app._toast_job = None
    app.toast.configure(text="")

    app._handle_line(
        "+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,"
        "ax=15,ay=-8,az=997,servo=96,lock=0,"
        "gx=37,gy=-18,gz=112,roll=-11.4,pitch=34.2")
    app.radar_var.set(True)
    app.stream_var.set(True)
    app.pcdata_var.set(True)
    app.lock_var.set(False)
    app.radar_on = True
    app.radar_disp = 128.0
    app.radar_prev = 121.0
    app.radar_angle = 128
    app.radar_last_dist = 74

    app.history = [round(24.4 + 0.7 * math.sin(i * 0.16)
                         + 0.18 * math.sin(i * 0.57), 2)
                   for i in range(hm.HISTORY)]
    app._draw_chart()

    for angle in range(8, 174, 5):
        if angle % 35 == 3:
            continue
        distance = int(92 + 58 * math.sin(angle * 0.045)
                       + 22 * math.cos(angle * 0.13))
        app.radar_pts[angle] = max(18, min(hm.RADAR_MAX_CM - 4, distance))
    app._draw_radar()

    app.cmd_var.set("SERVO,96")
    app.ts_var.set(True)
    app.crlf_var.set(True)
    app.hex_var.set(False)
    app.timer_var.set(False)
    app._clear_log()
    app.log.configure(state="normal", font=(hm.FONT_MONO, 9))
    for line in (
        "09:42:05 > GET",
        "09:42:05 < +DATA,light=72,temp=24.86,humi=58",
        "09:42:06 > SERVO,96",
    ):
        app.log.insert("end", line + "\n")
    app.log.yview_moveto(0.0)
    app.log.configure(state="disabled")


def main():
    if sys.platform != "win32":
        raise SystemExit("截图工具仅支持 Windows")

    hm.enable_dpi_awareness()
    # 固定输出 1375x975，避免不同机器缩放比例导致 README 图片漂移。
    hm._ui_scale_for = lambda root, work_w, work_h: 1.25
    if hm.sysinfo is not None:
        hm.sysinfo.Collector = DemoCollector

    root = tk.Tk()
    root.attributes("-topmost", True)
    app = hm.MonitorApp(root)
    app.sys = DemoCollector()
    populate(app)
    root.update_idletasks()
    root.update()

    app.scroll.yview_moveto(0)
    capture_client(root, ROOT / "screenshot.png")
    scroll_to(app, app.chart_head, margin=4)
    capture_client(root, ROOT / "screenshots" / "trends.png")

    # 特写使用更明显的 3/4 侧倾姿态，三轴方向更清晰。
    app.gyro_disp_roll = 30.0
    app.gyro_disp_pitch = -18.0
    app.gyro_target_roll = 30.0
    app.gyro_target_pitch = -18.0
    app.gyro_last_rx = time.monotonic()
    app._draw_gyro()
    capture_widget(app.gyro, ROOT / "screenshots" / "gyro.png")

    scroll_to(app, app.radar_head, margin=4)
    capture_client(root, ROOT / "screenshots" / "radar.png")

    root.destroy()


if __name__ == "__main__":
    main()
