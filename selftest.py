#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上位机离线自检 —— 不需要开发板、不需要串口。

用固件 bt.c 里真实的应答格式构造数据帧，喂给上位机的解析逻辑，
逐项核对：命令发得对不对、字段解析对不对、异常帧会不会崩。

用法：
    python selftest.py

全部通过退出码为 0，有失败为 1。
改了协议之后先跑一遍，再上硬件排查。
"""

import math
import sys
import time
import tkinter as tk

import host_monitor as hm


class FakeSerial:
    """只记录写入内容，不真开串口。"""

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def close(self):
        pass


class Checker:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.failures = []

    def check(self, desc, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  OK   {desc}")
        else:
            self.fail += 1
            self.failures.append(desc)
            print(f"  FAIL {desc}  {detail}")

    def summary(self):
        print(f"\n结果: {self.ok} 通过 / {self.fail} 失败")
        for f in self.failures:
            print(f"  失败项: {f}")
        return 1 if self.fail else 0


def main():
    root = tk.Tk()
    root.withdraw()
    app = hm.MonitorApp(root)
    app.ser = FakeSerial()
    app.active_baud = 9600
    c = Checker()

    print("[1] 发送侧：命令拼装（与 bt.c 的 bt_exec 对齐）")
    app.crlf_var.set(True)
    cases = [
        ("PING", b"PING\r\n"), ("GET", b"GET\r\n"),
        ("SERVO,90", b"SERVO,90\r\n"), ("LOCK,1", b"LOCK,1\r\n"),
        ("STREAM,1,500", b"STREAM,1,500\r\n"), ("BAUD,115200", b"BAUD,115200\r\n"),
        ("SYS", b"SYS\r\n"), ("VER", b"VER\r\n"), ("RESET", b"RESET\r\n"),
    ]
    for cmd, expect in cases:
        app._send(cmd)
        c.check(f"发出 {cmd}", app.ser.written[-1] == expect, app.ser.written[-1])

    app.crlf_var.set(False)
    app._send("PING")
    c.check("关闭自动回车后不加 CRLF", app.ser.written[-1] == b"PING", app.ser.written[-1])
    app.crlf_var.set(True)

    print("[2] 接收侧：+DATA 全字段")
    app._handle_line(
        "+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,"
        "az=997,servo=90,lock=1,gx=37,gy=-18,gz=112,roll=-11.4,pitch=34.2")
    d = app.data
    c.check("light", d["light"] == 72, d["light"])
    c.check("temp", abs(d["temp"] - 24.86) < 1e-6, d["temp"])
    c.check("humi", d["humi"] == 58, d["humi"])
    c.check("chip", abs(d["chip"] - 31.2) < 1e-6, d["chip"])
    c.check("vdd", d["vdd"] == 3284, d["vdd"])
    c.check("ax(mg)", d["ax"] == 15, d["ax"])
    c.check("gx", d["gx"] == 37, d["gx"])
    c.check("roll", abs(d["roll"] + 11.4) < 1e-6, d["roll"])
    c.check("pitch", abs(d["pitch"] - 34.2) < 1e-6, d["pitch"])
    c.check("lock 同步到复选框", app.lock_var.get() is True, app.lock_var.get())
    c.check("servo 同步到滑条", int(app.scale.get()) == 90, app.scale.get())
    c.check("左栏已移除舵机读数", "servo" not in app.value_labels, sorted(app.value_labels))
    c.check("左栏可滚动", app.scroll.winfo_exists() == 1, app.scroll)

    print("[3] 接收侧：$D 11 字段遥测帧")
    root.deiconify()
    root.update()
    app._handle_line("$D,80,2534,60,10,-5,998,40,-20,120,-120,300")
    c.check("light", d["light"] == 80, d["light"])
    c.check("temp=25.34", abs(d["temp"] - 25.34) < 1e-6, d["temp"])
    c.check("gz", d["gz"] == 120, d["gz"])
    c.check("roll=-12.0", abs(d["roll"] + 12.0) < 1e-6, d["roll"])
    c.check("pitch=30.0", abs(d["pitch"] - 30.0) < 1e-6, d["pitch"])
    bb = app.gyro.bbox("all")
    w, h = app.gyro.winfo_width(), app.gyro.winfo_height()
    c.check("陀螺仪画布不越界", not (bb[2] > w or bb[3] > h or bb[0] < 0 or bb[1] < 0),
            f"bbox={bb} canvas={w}x{h}")

    print("[4] 兼容性：旧固件 6 字段 $D")
    gx_before = d["gx"]
    app._handle_line("$D,50,2000,40,1,2,3")
    c.check("旧帧能正常解析", d["light"] == 50, d["light"])
    c.check("旧帧不影响陀螺仪字段", d["gx"] == gx_before, d["gx"])

    print("[5] 健壮性：异常帧不应抛异常")
    for bad in ["$D,", "$D,a,b,c,d,e,f", "+DATA,garbage", "+DATA,light=abc",
                "-ERR,LOCKED", "-ERR,BAUD", "+OK", "+OK,BAUD", "+PONG",
                "+SYS,rst=POR,up=1,baud=9600", "+VER,BASH-STM32,1.4.0", "乱码\x00\x01"]:
        try:
            app._handle_line(bad)
            res = "ok"
        except Exception as exc:  # noqa: BLE001
            res = f"异常 {exc}"
        c.check(f"处理 {bad!r}", res == "ok", res)

    print("[6] 波特率切换流程")
    app.baud_var.set("115200")
    app.ser.written.clear()
    app._sync_device_baud()
    c.check("在旧链路上发 BAUD", app.ser.written and app.ser.written[-1] == b"BAUD,115200\r\n",
            app.ser.written)
    c.check("挂起等待确认", app.pending_baud == 115200, app.pending_baud)
    app._handle_line("+OK,BAUD")
    c.check("确认后清除挂起", app.pending_baud == 0, app.pending_baud)

    print("[7] 雷达模式")
    app.radar_var.set(True)
    app.ser.written.clear()
    app._send_radar()
    root.update()
    c.check("开启时发 RADAR,1,60",
            app.ser.written and app.ser.written[-1] == b"RADAR,1,60\r\n", app.ser.written)
    c.check("雷达画布存在", app.radar.winfo_exists() == 1, app.radar)

    app._handle_line("$R,90,45")
    c.check("角度写入", app.radar_angle == 90, app.radar_angle)
    c.check("距离落点", app.radar_pts.get(90) == 45, app.radar_pts)

    app._handle_line("$R,120,-1")
    c.check("无回波不落点", 120 not in app.radar_pts, app.radar_pts)
    c.check("无回波仍更新角度", app.radar_angle == 120, app.radar_angle)

    app._handle_line("$R,999,10")
    c.check("角度越界被钳到 180", app.radar_angle == 180, app.radar_angle)

    root.update()
    bb = app.radar.bbox("all")
    w, h = app.radar.winfo_width(), app.radar.winfo_height()
    c.check("雷达画布不越界",
            bb is None or not (bb[2] > w or bb[3] > h or bb[0] < 0 or bb[1] < 0),
            f"bbox={bb} canvas={w}x{h}")

    app.radar_var.set(False)
    app.ser.written.clear()
    app._send_radar()
    c.check("关闭时发 RADAR,0",
            app.ser.written and app.ser.written[-1] == b"RADAR,0\r\n", app.ser.written)

    app._handle_line("+OK,RADAR=1")
    c.check("+OK,RADAR=1 回写复选框", app.radar_var.get() is True, app.radar_var.get())
    app._handle_line("+OK,RADAR=0")
    c.check("+OK,RADAR=0 清除复选框", app.radar_var.get() is False, app.radar_var.get())

    # ---------- 电脑数据帧 ----------
    payload = app._pc_payload()
    c.check("PC 帧以 PC, 开头", payload.startswith("PC,"), payload)

    fields = {}
    for item in payload.split(",")[1:]:
        k, _, v = item.partition("=")
        fields[k] = v
    need = ["cpu", "mem", "gpu", "ct", "gt", "cf", "gc", "ru", "rt", "dn", "up", "fps"]
    missing = [k for k in need if k not in fields]
    c.check("PC 帧字段齐全", not missing, f"缺少 {missing}")

    try:
        ints = {k: int(v) for k, v in fields.items()}
        all_int = True
    except (ValueError, TypeError):
        ints, all_int = {}, False
    c.check("PC 帧全是整数（MCU 端不用解析浮点）", all_int, fields)

    if all_int:
        c.check("占用率在 0-100",
                0 <= ints["cpu"] <= 100 and 0 <= ints["mem"] <= 100
                and (ints["gpu"] == -1 or 0 <= ints["gpu"] <= 100), ints)
        c.check("内存总量为正", ints["rt"] > 0, ints["rt"])

    c.check("PC 帧长度适合 9600 波特率", len(payload.encode()) <= 120,
            len(payload.encode()))

    # ---------- 固件不认识 PC 命令时的降噪 ----------
    # 推送期间 -ERR,UNKNOWN 应被吞掉，且只提示一次
    app.pcdata_var.set(True)
    app.pc_err_noted = False
    app.log.configure(state="normal")
    app.log.delete("1.0", "end")
    app.log.configure(state="disabled")
    app._handle_line("-ERR,UNKNOWN")
    app._handle_line("-ERR,UNKNOWN")
    txt = app.log.get("1.0", "end")
    recv = sum(1 for ln in txt.splitlines() if ln.strip().startswith("< -ERR"))
    c.check("推送期间吞掉 -ERR,UNKNOWN", recv == 0, txt.strip())
    c.check("固件不支持只提示一次", txt.count("固件暂不支持") == 1, txt.strip())

    # 不推送时不能误吞，正常错误要照常显示
    app.pcdata_var.set(False)
    app.log.configure(state="normal")
    app.log.delete("1.0", "end")
    app.log.configure(state="disabled")
    app._handle_line("-ERR,UNKNOWN")
    txt = app.log.get("1.0", "end")
    c.check("未推送时 -ERR 照常记录", "< -ERR,UNKNOWN" in txt, txt.strip())

    print("[8] 3D 姿态仪：数据触发、缓动、异常与断连")
    app.gyro_link = True
    app.gyro_has_data = False
    app._handle_line(
        "+DATA,light=70,temp=24.5,humi=55,chip=30.1,vdd=3290,"
        "ax=10,ay=-5,az=998,servo=90,lock=0,"
        "gx=12,gy=-8,gz=28,roll=18.5,pitch=-7.2")
    c.check("+DATA 更新姿态目标",
            abs(app.gyro_target_roll - 18.5) < 1e-6
            and abs(app.gyro_target_pitch + 7.2) < 1e-6,
            (app.gyro_target_roll, app.gyro_target_pitch))
    c.check("首帧直接对齐显示姿态",
            abs(app.gyro_disp_roll - 18.5) < 1e-6,
            app.gyro_disp_roll)
    c.check("在线状态为 LIVE", app._gyro_status()[0] == "LIVE",
            app._gyro_status())

    base = time.monotonic()
    app.gyro_anim_at = base
    app._set_gyro_target(30, -10, 45, -20, 12)
    old_roll = app.gyro_disp_roll
    app._step_gyro(base + 0.033)
    c.check("单帧 33ms 即产生可感知响应",
            1.0 < abs(app.gyro_disp_roll - old_roll) < 38.5,
            app.gyro_disp_roll - old_roll)
    for i in range(1, 17):
        app._step_gyro(base + i * 0.033)
    c.check("约 0.5 秒平滑收敛到目标",
            abs(app.gyro_disp_roll + 20) < 0.4
            and abs(app.gyro_disp_pitch - 12) < 0.4,
            (app.gyro_disp_roll, app.gyro_disp_pitch))

    app.gyro_disp_roll = 179.0
    app.gyro_anim_at = base + 1.0
    app._set_gyro_target(0, 0, 0, -179, 0)
    app._step_gyro(base + 1.033)
    c.check("跨越 ±180° 时走最短方向",
            app.gyro_disp_roll > 179.0,
            app.gyro_disp_roll)

    roll_before = app.gyro_target_roll
    app._set_gyro_target(0, 0, 0, math.nan, 0)
    c.check("NaN 姿态不污染目标",
            app.gyro_target_roll == roll_before,
            (app.gyro_target_roll, roll_before))
    c.check("非法帧显示 DATA ERR",
            app._gyro_status()[0] == "DATA ERR",
            app._gyro_status())

    app._set_gyro_target(0, 0, 0, 5, 5)
    safe_roll = app.gyro_target_roll
    try:
        app._handle_line(
            "+DATA,light=70,temp=24.5,humi=55,chip=30.1,vdd=3290,"
            "ax=10,ay=-5,az=998,servo=90,lock=0,"
            "gx=0,gy=0,gz=0,roll=nan,pitch=5")
        parser_ok = True
    except Exception:  # noqa: BLE001
        parser_ok = False
    c.check("+DATA 中的 NaN 不抛异常", parser_ok, parser_ok)
    c.check("+DATA 中的 NaN 保留上一目标",
            app.gyro_target_roll == safe_roll,
            (app.gyro_target_roll, safe_roll))

    app._set_gyro_target(0, 0, 0, 270, 120)
    c.check("横滚归一化且俯仰钳位",
            abs(app.gyro_target_roll + 90) < 1e-6
            and app.gyro_target_pitch == 90,
            (app.gyro_target_roll, app.gyro_target_pitch))

    app.gyro_last_rx = time.monotonic() - hm.GYRO_STALE_S - 0.2
    c.check("超过 2 秒无帧显示 HOLD",
            app._gyro_status()[0] == "HOLD", app._gyro_status())
    app.gyro_link = False
    c.check("断连显示 OFFLINE",
            app._gyro_status()[0] == "OFFLINE", app._gyro_status())

    root.destroy()
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
