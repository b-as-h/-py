#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""3D 姿态仪模拟器：不需要 STM32 或串口。

每 700ms 注入一帧真实格式的 +DATA，完整经过 _handle_line 解析、目标更新和
30fps 动画链路。姿态先小幅变化再大幅摆动，最后回到中位，便于检查灵敏度和
回中效果。

用法：
    python tools/gyro_sim.py
"""

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host_monitor as hm  # noqa: E402


class FakeCollector:
    def __init__(self, interval=1.0):
        pass

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
        return 0

    def close(self):
        pass


def frame(roll, pitch, gx, gy, gz):
    """按固件 GET 的完整字段格式生成虚拟应答。"""
    return (
        f"+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,"
        f"ax=15,ay=-8,az=997,servo=96,lock=0,"
        f"gx={gx},gy={gy},gz={gz},roll={roll},pitch={pitch}"
    )


# 静止 → 5° → 15° → 30° → 反向 30° → 俯仰组合 → 回中。
# 数据全部是整数或一位小数，与 README 中的 +DATA 字段一致。
DEMO_FRAMES = [
    frame(0.0, 0.0, 0, 0, 0),
    frame(5.0, 0.0, 0, 0, 15),
    frame(15.0, -4.0, 0, 0, 28),
    frame(30.0, -8.0, 0, 0, 45),
    frame(-18.0, 10.0, 20, -14, -38),
    frame(-32.0, 18.0, 35, -22, -62),
    frame(0.0, 0.0, 0, 0, 0),
]


def main():
    if sys.platform != "win32":
        raise SystemExit("模拟器依赖 Windows 桌面环境")

    hm.enable_dpi_awareness()
    if hm.sysinfo is not None:
        hm.sysinfo.Collector = FakeCollector

    root = tk.Tk()
    app = hm.MonitorApp(root)
    root.title("STM32 Host · 3D 姿态模拟")
    app.sys = FakeCollector()
    app.ser = FakeSerial()
    app.gyro_link = True
    app.active_baud = 115200
    app.port_box["values"] = ("SIM  HC-05 模拟链路",)
    app.port_var.set("SIM  HC-05 模拟链路")
    app.baud_var.set("115200")
    app.connect_btn.configure(text="停止模拟")
    app._set_status("模拟链路 · 700ms/帧", "ok")
    app.stream_var.set(True)
    app.radar_var.set(False)
    app.radar_on = False
    app.lock_var.set(False)
    app.cmd_var.set("GET")
    app.history = [24.0 + (i % 7) * 0.12 for i in range(hm.HISTORY)]
    app._draw_chart()
    app._handle_line(DEMO_FRAMES[0])
    idx = 1

    def tick():
        nonlocal idx
        app._handle_line(DEMO_FRAMES[idx % len(DEMO_FRAMES)])
        idx += 1
        root.after(700, tick)

    root.after(700, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
