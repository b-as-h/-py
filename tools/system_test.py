#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级前台自动化测试。

使用虚拟串口和虚拟性能快照驱动真实 Tk 界面，覆盖协议、控件、数据展示、
3D 姿态仪、温度曲线、雷达和异常恢复。三个可视化模块分别保持至少 5 秒，
测试台与主界面实时更新，结束后生成 Markdown/JSON 报告和阶段截图。

用法：
    python tools/system_test.py
    python tools/system_test.py --hold
"""

import argparse
import ctypes
import json
import math
import platform
import re
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import host_monitor as hm  # noqa: E402
import capture_screenshots as capture  # noqa: E402

RESULT_DIR = ROOT / "test-results"
REPORT_DIR = ROOT / "docs"
LONG_STAGES = ("陀螺仪", "温度曲线", "超声波雷达")


class DemoCollector:
    """可控且可复现的电脑性能快照。"""

    def __init__(self, interval=1.0):
        self.interval = interval
        self.tick = 0

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        self.tick += 1
        level = 34 + int(8 * math.sin(self.tick * 0.4))
        return {
            "cpu": float(level), "mem": 62.0, "gpu": 27.0,
            "cpu_temp": 54.0, "gpu_temp": 48.0,
            "cpu_mhz": 4380.0, "gpu_mhz": 1860.0,
            "ram_used": 19.8, "ram_total": 31.9,
            "net_down": 126.4, "net_up": 18.2,
            "fps": 144.0, "hz": 144.0,
        }


class FakeSerial:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True


class TestHud:
    """独立测试台，不遮挡主界面数据区。"""

    def __init__(self, root, checker):
        self.root = root
        self.checker = checker
        self.win = tk.Toplevel(root)
        self.win.title("SYSTEM TEST · LIVE")
        self.win.configure(bg="#0c1218")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)

        self.title = tk.Label(self.win, text="SYSTEM TEST", bg="#0c1218",
                              fg="#4fc3a1", font=("Microsoft YaHei UI", 12, "bold"),
                              anchor="w")
        self.title.pack(fill="x", padx=14, pady=(12, 2))
        self.stage = tk.Label(self.win, text="准备中", bg="#0c1218",
                              fg="#edf3f7", font=("Microsoft YaHei UI", 11, "bold"),
                              anchor="w")
        self.stage.pack(fill="x", padx=14)
        self.timer = tk.Label(self.win, text="0.0 / 0.0s", bg="#0c1218",
                              fg="#93a4b3", font=("Consolas", 9), anchor="w")
        self.timer.pack(fill="x", padx=14, pady=(2, 6))

        self.progress = tk.Canvas(self.win, width=390, height=6, bg="#1a222b",
                                  highlightthickness=0)
        self.progress.pack(fill="x", padx=14, pady=(0, 9))
        self.progress.create_rectangle(0, 0, 0, 6, fill="#4fc3a1", outline="",
                                       tags="bar")

        self.live = tk.Label(self.win, text="", bg="#141c24", fg="#cfe0e8",
                             font=("Consolas", 9), justify="left", anchor="w",
                             padx=8, pady=6)
        self.live.pack(fill="x", padx=14, pady=(0, 8))
        self.stats = tk.Label(self.win, text="PASS 0 · FAIL 0", bg="#0c1218",
                              fg="#edf3f7", font=("Consolas", 10, "bold"),
                              anchor="w")
        self.stats.pack(fill="x", padx=14, pady=(0, 4))

        self.log = tk.Text(self.win, height=9, width=50, bg="#101820",
                           fg="#aebdc8", bd=0, font=("Consolas", 8),
                           state="disabled", padx=6, pady=4)
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.win.protocol("WM_DELETE_WINDOW", root.destroy)

        root.update_idletasks()
        sw = root.winfo_screenwidth()
        hx = root.winfo_rootx() + root.winfo_width() + 14
        if hx + 430 > sw:
            hx = max(8, root.winfo_rootx() - 444)
        hy = max(8, root.winfo_rooty())
        self.win.geometry(f"420x370+{hx}+{hy}")

    def set_stage(self, name, duration):
        self.stage.configure(text=name)
        self.timer.configure(text=f"0.0 / {duration:.1f}s")

    def update_progress(self, elapsed, duration):
        self.timer.configure(text=f"{elapsed:.1f} / {duration:.1f}s")
        self.progress.coords("bar", 0, 0,
                             390 * min(1.0, elapsed / max(0.1, duration)), 6)
        self.stats.configure(
            text=f"PASS {self.checker.ok} · FAIL {self.checker.fail}")

    def set_live(self, text):
        self.live.configure(text=text)

    def add_result(self, module, name, ok, detail):
        self.log.configure(state="normal")
        mark = "PASS" if ok else "FAIL"
        self.log.insert("end", f"{mark} [{module}] {name}\n")
        if not ok:
            self.log.insert("end", f"     {detail}\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 120:
            self.log.delete("1.0", "30.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.stats.configure(
            text=f"PASS {self.checker.ok} · FAIL {self.checker.fail}")


class Checker:
    def __init__(self, hud):
        self.hud = hud
        self.ok = 0
        self.fail = 0
        self.records = []

    def check(self, module, name, condition, detail=""):
        passed = bool(condition)
        if passed:
            self.ok += 1
        else:
            self.fail += 1
        detail = str(detail)
        self.records.append({
            "module": module, "case": name, "passed": passed, "detail": detail,
        })
        if self.hud:
            self.hud.add_result(module, name, passed, detail)
        return passed


class Harness:
    def __init__(self, root, app, unit_result):
        self.root = root
        self.app = app
        self.unit_result = unit_result
        self.hud = TestHud(root, None)
        self.c = Checker(self.hud)
        self.hud.checker = self.c
        self.stage_records = []
        self.stage_index = 0
        self.elapsed = 0.0
        self.stage_started = 0.0
        self.current = None
        self.finished = False
        self.long_stats = {}
        self.gyro_frames = 0
        self.temp_frames = 0
        self.radar_frames = 0

        self.stages = [
            dict(name="基础通信与控件", module="基础通信", duration=0.9,
                 start=self._base_start, tick=self._base_tick,
                 done=self._base_done, live=self._base_live),
            dict(name="陀螺仪 · 5 秒动态与状态迁移", module="陀螺仪",
                 duration=6.4, start=self._gyro_start, tick=self._gyro_tick,
                 done=self._gyro_done, live=self._gyro_live),
            dict(name="温度曲线 · 5 秒持续采样", module="温度曲线",
                 duration=5.6, start=self._temp_start, tick=self._temp_tick,
                 done=self._temp_done, live=self._temp_live),
            dict(name="超声波雷达 · 5 秒扫描", module="超声波雷达",
                 duration=5.6, start=self._radar_start, tick=self._radar_tick,
                 done=self._radar_done, live=self._radar_live),
            dict(name="断连、恢复与最终汇总", module="异常恢复",
                 duration=1.2, start=self._recovery_start,
                 tick=self._noop, done=self._recovery_done,
                 live=self._recovery_live),
        ]

    # ---------- 公共辅助 ----------
    def _rec(self, module, name, condition, detail=""):
        return self.c.check(module, name, condition, detail)

    def _log(self, text):
        self.app._log(f"[TEST] {text}")

    def _scroll_to(self, widget):
        self.app.scroll.update_idletasks()
        top = (widget.winfo_rooty() - self.app.scroll.winfo_rooty()
               + self.app.scroll.canvasy(0) - hm._sp(8))
        total = max(1, self.app.scroll.bbox("all")[3])
        self.app.scroll.yview_moveto(max(0.0, top / total))

    def _write_exists(self, payload):
        return payload in self.app.ser.written

    @staticmethod
    def _full_data(light, temp, humi, chip, vdd, ax, ay, az,
                   servo, lock, gx, gy, gz, roll, pitch):
        return (
            f"+DATA,light={light},temp={temp},humi={humi},chip={chip},vdd={vdd},"
            f"ax={ax},ay={ay},az={az},servo={servo},lock={lock},"
            f"gx={gx},gy={gy},gz={gz},roll={roll},pitch={pitch}")

    @staticmethod
    def _telemetry(temp, gx=0, gy=0, gz=0, roll=0, pitch=0):
        return (
            f"$D,72,{int(round(temp * 100))},58,15,-8,997,"
            f"{gx},{gy},{gz},{int(round(roll * 10))},{int(round(pitch * 10))}")

    # ---------- 运行器 ----------
    def start(self):
        self._record_unit()
        self._start_stage(0)

    def _record_unit(self):
        module = "协议单元回归"
        self._rec(
            module, "selftest.py 完整回归",
            self.unit_result["passed"],
            self.unit_result["summary"])
        self._log(f"单元回归：{self.unit_result['summary']}")

    def _start_stage(self, index):
        if index >= len(self.stages):
            self._finish()
            return
        self.stage_index = index
        self.current = self.stages[index]
        self.elapsed = 0.0
        self.stage_started = time.monotonic()
        stage = self.current
        self.hud.set_stage(stage["name"], stage["duration"])
        self.app._set_status(f"测试中 · {stage['name']}", "warn")
        self._log(f"开始：{stage['name']}")
        try:
            stage["start"]()
        except Exception as exc:  # noqa: BLE001
            self._rec(stage["module"], "阶段启动异常", False, repr(exc))
        self.root.after(25, self._pump)

    def _pump(self):
        if self.finished:
            return
        stage = self.current
        self.elapsed = time.monotonic() - self.stage_started
        try:
            stage["tick"](self.elapsed)
            self.hud.set_live(stage["live"]())
        except Exception as exc:  # noqa: BLE001
            self._rec(stage["module"], "阶段运行异常", False, repr(exc))
        self.hud.update_progress(self.elapsed, stage["duration"])
        self.app._set_status(
            f"测试中 · {stage['module']}  {self.elapsed:.1f}/{stage['duration']:.1f}s",
            "warn")
        if self.elapsed < stage["duration"]:
            self.root.after(25, self._pump)
            return

        actual = self.elapsed
        try:
            stage["done"](actual)
        except Exception as exc:  # noqa: BLE001
            self._rec(stage["module"], "阶段断言异常", False, repr(exc))
        self.stage_records.append({
            "name": stage["name"], "module": stage["module"],
            "duration": round(actual, 3),
        })
        self.root.after(300, lambda: self._start_stage(self.stage_index + 1))

    def _noop(self, _elapsed):
        pass

    def _base_live(self):
        return (f"serial={'OK' if self.app.ser else 'NO'}  "
                f"status={self.app.status.cget('text')}")

    # ---------- 阶段 1：基础通信与控件 ----------
    def _base_start(self):
        app = self.app
        app.port_box["values"] = ("COM5  HC-05 (SPP)",)
        app.port_var.set("COM5  HC-05 (SPP)")
        app.baud_var.set("115200")
        app.active_baud = 115200
        app.connect_btn.configure(text="断开连接")
        app.gyro_link = True
        app._clear_log()
        app.ser.written.clear()

        self._rec("界面与 DPI", "主窗口和三栏完成构建",
                  app.top_card.winfo_exists() and app.right_panel.winfo_exists()
                  and app.canvas.winfo_exists())
        self._rec("界面与 DPI", "UI 缩放已计算", app.ui_scale > 0,
                  f"scale={app.ui_scale:.3f}")
        app._draw_sysinfo()
        self._rec("电脑状态", "性能圆环绘制",
                  len(app.sys_canvas.find_all()) > 10,
                  len(app.sys_canvas.find_all()))

        app._handle_line("+PONG")
        self._rec("串口与协议", "PONG 更新在线状态",
                  "online" in app.status.cget("text").lower() or
                  "在线" in app.status.cget("text"),
                  app.status.cget("text"))

        app._send("PING")
        self._rec("串口与协议", "PING 命令编码",
                  self._write_exists(b"PING\r\n"))

        app.crlf_var.set(False)
        app._send("GET")
        self._rec("串口与协议", "关闭 CRLF 后原样发送",
                  self._write_exists(b"GET"))
        app.crlf_var.set(True)

        self._rec("串口与协议", "HEX 合法输入",
                  app._parse_hex("AA 55 01") == b"\xaaU\x01",
                  app._parse_hex("AA 55 01"))
        self._rec("串口与协议", "HEX 非法输入返回 None",
                  app._parse_hex("GG") is None)

        app.hex_var.set(True)
        app.cmd_var.set("AA 55 01")
        app.ser.written.clear()
        app._send_custom()
        self._rec("串口与协议", "HEX 界面发送实际字节",
                  self._write_exists(b"\xaaU\x01"))
        app.cmd_var.set("ZZ")
        app.ser.written.clear()
        app._send_custom()
        self._rec("串口与协议", "非法 HEX 不发送",
                  not app.ser.written)
        app.hex_var.set(False)

        app.cmd_var.set("PING")
        app._send_custom()
        self._rec("串口与协议", "命令历史记录与回填",
                  "PING" in app.cmd_history, app.cmd_history[-3:])
        app.cmd_var.set("")
        app._hist_prev(None)
        self._rec("串口与协议", "↑ 历史命令回填",
                  app.cmd_var.get() == app.cmd_history[-1],
                  app.cmd_var.get())

        app.baud_var.set("19200")
        app.active_baud = 115200
        app.ser.written.clear()
        app._sync_device_baud()
        self._rec("串口与协议", "同步波特率走旧链路发送",
                  self._write_exists(b"BAUD,19200\r\n")
                  and app.pending_baud == 19200,
                  app.pending_baud)
        app.pending_baud = 0
        app.baud_deadline = 0
        app.baud_var.set("115200")
        app.active_baud = 115200

        app.pcdata_var.set(True)
        app.pc_err_noted = False
        app.log.configure(state="normal")
        app.log.delete("1.0", "end")
        app.log.configure(state="disabled")
        app._handle_line("-ERR,UNKNOWN")
        self._rec("电脑状态", "不支持 PC 时只提示一次",
                  app.pc_err_noted
                  and app.log.get("1.0", "end").count("固件暂不支持") == 1)
        app.pcdata_var.set(False)

        app._handle_line("+VER,BASH-STM32,1.4.0")
        app._handle_line("-ERR,LOCKED")
        log_text = app.log.get("1.0", "end")
        self._rec("日志与反馈", "版本与错误应答进入日志",
                  "+VER" in log_text and "-ERR,LOCKED" in log_text)
        app.ts_var.set(True)
        app._clear_log()
        app._log("timestamp check")
        stamped = app.log.get("1.0", "end").strip()
        self._rec("日志与反馈", "时间戳前缀与清空功能",
                  re.match(r"\d{2}:\d{2}:\d{2} timestamp check", stamped)
                  is not None, stamped)

        saved_ser = app.ser
        app.ser = None
        app.ser_written = list(saved_ser.written)
        ok = app._send("PING")
        self._rec("异常恢复", "未连接时发送被拒绝", ok is False)
        app.ser = saved_ser
        app.ser.written = app.ser_written
        del app.ser_written

        app._handle_line(self._full_data(
            60, 22.0, 48, 30.0, 3250, 0, 0, 1000,
            90, 0, 0, 0, 0, 0, 0))

        app.scale.set(125)
        app._send_servo()
        self._rec("舵机与控制", "滑条发送 SERVO,125",
                  self._write_exists(b"SERVO,125\r\n"))
        app.lock_var.set(True)
        app._send_lock()
        self._rec("舵机与控制", "锁定发送 LOCK,1",
                  self._write_exists(b"LOCK,1\r\n"))
        app.stream_var.set(True)
        app._send_stream()
        self._rec("舵机与控制", "遥测发送 STREAM,1,500",
                  self._write_exists(b"STREAM,1,500\r\n"))
        app.lock_var.set(False)

        app._handle_line(self._full_data(
            82, 25.34, 61, 31.2, 3284, 15, -8, 997,
            125, 0, 37, -18, 112, -11.4, 34.2))
        self._rec("传感器数据", "+DATA 全字段解析",
                  app.data["light"] == 82 and abs(app.data["temp"] - 25.34) < 1e-6
                  and app.data["lock"] == 0 and app.data["servo"] == 125,
                  {k: app.data[k] for k in ("light", "temp", "servo", "lock")})
        self._rec("传感器数据", "传感器标签同步",
                  app.value_labels["light"].cget("text") == "82",
                  app.value_labels["light"].cget("text"))

        app._handle_line(self._full_data(
            100, 85.0, 99, 60.0, 3600, 2000, -2000, 2000,
            180, 1, 0, 0, 1000, 180, 90))
        self._rec("传感器数据", "边界字段接收",
                  app.data["servo"] == 180 and app.data["lock"] == 1
                  and app.gyro_target_pitch == 90,
                  (app.data["servo"], app.gyro_target_pitch))

        before = app.gyro_target_roll
        try:
            app._handle_line(self._full_data(
                50, 20.0, 40, 30.0, 3200, 0, 0, 0,
                90, 0, 0, 0, 0, "nan", 0))
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        self._rec("异常数据", "NaN 帧不抛异常且不污染姿态",
                  ok and app.gyro_target_roll == before,
                  app.gyro_target_roll)

        app._handle_line("$D,62,2150,55,5,-3,995")
        self._rec("传感器数据", "旧版 6 字段 $D 兼容",
                  abs(app.data["temp"] - 21.5) < 1e-6,
                  app.data["temp"])
        app._handle_line(self._telemetry(28.56, 14, -9, 22, 12.3, -4.5))
        self._rec("传感器数据", "11 字段 $D 遥测解析",
                  abs(app.data["temp"] - 28.56) < 1e-6
                  and abs(app.data["pitch"] + 4.5) < 1e-6,
                  (app.data["temp"], app.data["pitch"]))

        app.history.clear()
        for i in range(hm.HISTORY + 5):
            app._push_history(20.0 + i * 0.1)
        self._rec("温度曲线", "历史缓存上限 120",
                  len(app.history) == hm.HISTORY, len(app.history))
        app.history.clear()
        app._draw_chart()

        payload = app._pc_payload()
        try:
            fields = dict(x.split("=") for x in payload.split(",")[1:])
            ints_ok = all(str(int(v)) == v for v in fields.values())
        except Exception:  # noqa: BLE001
            ints_ok = False
        self._rec("电脑状态", "PC 帧全整数且字段完整",
                  ints_ok and len(fields) == 12 and len(payload) <= 120,
                  len(payload))

        app._handle_line("+VER,BASH-STM32,1.4.0")
        app._handle_line("-ERR,LOCKED")
        log_text = app.log.get("1.0", "end")
        self._rec("日志与反馈", "版本与错误应答进入日志",
                  "+VER" in log_text and "-ERR,LOCKED" in log_text)

    def _base_tick(self, _elapsed):
        pass

    def _base_done(self, actual):
        self._rec("基础通信", "基础阶段时长有效", actual >= 0.8,
                  f"{actual:.2f}s")

    # ---------- 阶段 2：陀螺仪 ----------
    def _gyro_start(self):
        app = self.app
        self.gyro_frames = 0
        self.gyro_next_feed = 0.0
        self.gyro_reported_error = False
        self.gyro_states = set()
        self.gyro_poses = []
        self.gyro_shot = False
        app.gyro_link = True
        app.gyro_has_data = False
        app.gyro_disp_roll = 0.0
        app.gyro_disp_pitch = 0.0
        app.gyro_target_roll = 0.0
        app.gyro_target_pitch = 0.0
        app.gyro_last_rx = time.monotonic()
        self._scroll_to(app.chart_head)
        app._clear_log()
        self._log("陀螺仪：20Hz 虚拟姿态 → 2 秒静默 → 非法帧 → 断连")

    def _gyro_tick(self, elapsed):
        app = self.app
        limit = min(elapsed, 2.6)
        while self.gyro_next_feed <= limit:
            t = self.gyro_next_feed
            roll = 26.0 * math.sin(t * 2.2)
            pitch = 16.0 * math.sin(t * 1.35 + 0.5)
            gx = int(48 * math.cos(t * 2.2))
            gy = int(-30 * math.sin(t * 1.35))
            gz = int(24 * math.cos(t * 0.7))
            app._handle_line(self._full_data(
                72, 24.86, 58, 31.2, 3284, 15, -8, 997,
                96, 0, gx, gy, gz, f"{roll:.1f}", f"{pitch:.1f}"))
            self.gyro_frames += 1
            self.gyro_poses.append((app.gyro_disp_roll, app.gyro_disp_pitch))
            self.gyro_next_feed += 0.05
        if elapsed >= 3.2 and not self.gyro_shot:
            self.gyro_shot = True
            capture.capture_client(self.root, RESULT_DIR / "system_gyro.png")
        if elapsed >= 4.8 and not self.gyro_reported_error:
            self.gyro_reported_error = True
            app._handle_line(self._full_data(
                72, 24.86, 58, 31.2, 3284, 15, -8, 997,
                96, 0, 0, 0, 0, "nan", 0))
        if elapsed >= 5.6:
            app.gyro_link = False
            app._draw_gyro()
        self.gyro_states.add(app._gyro_status()[0])

    def _gyro_live(self):
        states = "/".join(sorted(self.gyro_states)) or "-"
        return (f"frames={self.gyro_frames}  roll={self.app.gyro_disp_roll:+.1f}°  "
                f"pitch={self.app.gyro_disp_pitch:+.1f}°\n"
                f"state={self.app._gyro_status()[0]}  seen={states}")

    def _gyro_done(self, actual):
        moved = sum(1 for a, b in zip(self.gyro_poses, self.gyro_poses[1:])
                    if abs(a[0] - b[0]) > 0.05 or abs(a[1] - b[1]) > 0.05)
        states = self.gyro_states
        self._rec("陀螺仪", "持续展示不少于 5 秒", actual >= 5.0,
                  f"{actual:.2f}s")
        self._rec("陀螺仪", "20Hz 连续采样并驱动姿态", self.gyro_frames >= 30,
                  self.gyro_frames)
        self._rec("陀螺仪", "姿态画面持续变化", moved >= 20, moved)
        self._rec("陀螺仪", "画布内容有效无溢出",
                  len(self.app.gyro.find_all()) >= 20 and
                  self._bbox_inside(self.app.gyro),
                  len(self.app.gyro.find_all()))
        self._rec("陀螺仪", "状态覆盖 LIVE/HOLD/DATA ERR/OFFLINE",
                  {"LIVE", "HOLD", "DATA ERR", "OFFLINE"}.issubset(states),
                  sorted(states))

        # 恢复用于后续阶段
        self.app.gyro_link = True
        self.app._set_gyro_target(0, 0, 0, 0, 0)
        self.app._set_status("测试继续 · 虚拟链路已恢复", "warn")
        self.long_stats["陀螺仪"] = {
            "duration": round(actual, 2), "frames": self.gyro_frames,
            "states": sorted(states), "shot": "test-results/system_gyro.png",
        }

    @staticmethod
    def _bbox_inside(canvas):
        box = canvas.bbox("all")
        if box is None:
            return False
        return not (box[0] < 0 or box[1] < 0
                    or box[2] > canvas.winfo_width()
                    or box[3] > canvas.winfo_height())

    # ---------- 阶段 3：温度曲线 ----------
    def _temp_start(self):
        app = self.app
        self.temp_frames = 0
        self.temp_next_feed = 0.0
        self.temp_malformed_before = 0
        self.temp_bad_sent = False
        self.temp_shot = False
        self.temp_values = []
        app.history.clear()
        app._draw_chart()
        self._scroll_to(app.chart_head)
        app._clear_log()
        self._log("温度曲线：5 秒内按 2Hz 注入 18–42℃，最后发送坏帧")

    def _temp_tick(self, elapsed):
        app = self.app
        limit = min(elapsed, 5.0)
        while self.temp_next_feed <= limit:
            t = self.temp_next_feed
            temp = 24.0 + 14.0 * math.sin(self.temp_frames * 0.55 - 1.1)
            self.temp_values.append(temp)
            app._handle_line(self._telemetry(
                temp,
                int(20 * math.sin(t * 1.7)),
                int(-14 * math.cos(t * 1.1)),
                int(30 * math.sin(t * 0.8)),
                20 * math.sin(t * 0.9),
                -12 * math.cos(t * 0.6)))
            self.temp_frames += 1
            self.temp_next_feed += 0.4
        if elapsed >= 3.8 and not self.temp_shot:
            self.temp_shot = True
            capture.capture_client(
                self.root, RESULT_DIR / "system_temperature.png")
        if elapsed >= 5.2 and not self.temp_bad_sent:
            self.temp_bad_sent = True
            self.temp_malformed_before = len(app.history)
            app._handle_line(f"$D,{int(self.temp_values[-1] * 100)},bad,58")
        self.app._set_status(
            f"测试中 · 温度曲线 {elapsed:.1f}/{self.current['duration']:.1f}s",
            "warn")

    def _temp_live(self):
        value = self.app.history[-1] if self.app.history else 0.0
        lo = min(self.app.history) if self.app.history else 0.0
        hi = max(self.app.history) if self.app.history else 0.0
        return (f"frames={self.temp_frames}  history={len(self.app.history)}\n"
                f"temp={value:.2f}℃  range={lo:.1f}–{hi:.1f}℃")

    def _temp_done(self, actual):
        app = self.app
        self._rec("温度曲线", "持续展示不少于 5 秒", actual >= 5.0,
                  f"{actual:.2f}s")
        self._rec("温度曲线", "2.5Hz 持续采样更新", self.temp_frames >= 10,
                  self.temp_frames)
        self._rec("温度曲线", "温度历史逐帧增长", len(app.history) >= 10,
                  len(app.history))
        self._rec("温度曲线", "曲线已绘制",
                  len(app.canvas.find_all()) >= 3,
                  len(app.canvas.find_all()))
        self._rec("温度曲线", "坏帧不追加数据点",
                  len(app.history) == self.temp_malformed_before,
                  (len(app.history), self.temp_malformed_before))
        self._rec("温度曲线", "温度范围符合注入数据",
                  min(app.history) < 15 and max(app.history) > 33,
                  (min(app.history), max(app.history)))
        self._rec("温度曲线", "画布内容无溢出",
                  self._bbox_inside(app.canvas))
        self.long_stats["温度曲线"] = {
            "duration": round(actual, 2), "frames": self.temp_frames,
            "points": len(app.history),
            "range_c": [round(min(app.history), 2), round(max(app.history), 2)],
            "shot": "test-results/system_temperature.png",
        }

    # ---------- 阶段 4：超声波雷达 ----------
    def _radar_start(self):
        app = self.app
        self.radar_frames = 0
        self.radar_next_feed = 0.0
        self.radar_angles = []
        self.radar_bad_distance = False
        self.radar_clamped = False
        self.radar_shot = False
        app.radar_var.set(True)
        app.ser.written.clear()
        app._send_radar()
        self._scroll_to(app.radar_head)
        app._clear_log()
        self._log("雷达：20Hz 角度扫描，包含 -1 无回波与超量程距离")

    def _radar_tick(self, elapsed):
        app = self.app
        limit = min(elapsed, 5.2)
        while self.radar_next_feed <= limit:
            t = self.radar_next_feed
            idx = self.radar_frames
            angle = int((t * 42) % 180)
            dist = 28 + (idx * 7) % 175
            if idx % 11 == 0:
                dist = -1
            elif idx % 17 == 0:
                dist = 250
                self.radar_bad_distance = True
            app._handle_line(f"$R,{angle},{dist}")
            if idx == 24:
                app._handle_line("$R,999,64")
                self.radar_clamped = True
            self.radar_angles.append(app.radar_angle)
            self.radar_frames += 1
            self.radar_next_feed += 0.05
        if elapsed >= 3.4 and not self.radar_shot:
            self.radar_shot = True
            capture.capture_client(self.root, RESULT_DIR / "system_radar.png")
        self.app._set_status(
            f"测试中 · 雷达 {elapsed:.1f}/{self.current['duration']:.1f}s",
            "warn")

    def _radar_live(self):
        return (f"frames={self.radar_frames}  angle={self.app.radar_angle}°\n"
                f"points={len(self.app.radar_pts)}  "
                f"distance={self.app.radar_last_dist}cm  on={self.app.radar_on}")

    def _radar_done(self, actual):
        app = self.app
        self._rec("超声波雷达", "持续展示不少于 5 秒", actual >= 5.0,
                  f"{actual:.2f}s")
        self._rec("超声波雷达", "20Hz 帧持续更新", self.radar_frames >= 80,
                  self.radar_frames)
        self._rec("超声波雷达", "扫描角度发生变化",
                  len(set(self.radar_angles)) > 60,
                  len(set(self.radar_angles)))
        self._rec("超声波雷达", "有效回波点已绘制",
                  len(app.radar_pts) >= 20, len(app.radar_pts))
        self._rec("超声波雷达", "无回波值 -1 未写入点",
                  all(v >= 0 for v in app.radar_pts.values()))
        self._rec("超声波雷达", "超量程点被限制显示",
                  self.radar_bad_distance and
                  all(v <= 400 for v in app.radar_pts.values()))
        self._rec("超声波雷达", "越界角度被钳位",
                  self.radar_clamped and max(self.radar_angles) <= 180,
                  max(self.radar_angles))
        self._rec("超声波雷达", "雷达画布无溢出",
                  self._bbox_inside(app.radar))

        app.radar_var.set(False)
        app.ser.written.clear()
        app._send_radar()
        self._rec("超声波雷达", "雷达关闭命令正确",
                  b"RADAR,0\r\n" in app.ser.written and not app.radar_on,
                  app.ser.written[-1] if app.ser.written else "--")
        self.long_stats["超声波雷达"] = {
            "duration": round(actual, 2), "frames": self.radar_frames,
            "angles": len(set(self.radar_angles)), "points": len(app.radar_pts),
            "shot": "test-results/system_radar.png",
        }

    # ---------- 阶段 5：断连与恢复 ----------
    def _recovery_start(self):
        app = self.app
        app.radar_var.set(False)
        app.radar_on = False
        app.ser = None
        app._disconnect(silent=True)
        self._log("断连：验证 OFFLINE；随后恢复虚拟链路和 LIVE")
        app.ser = FakeSerial()
        app.gyro_link = False
        app._draw_gyro()
        self._rec("异常恢复", "断连状态 OFFLINE",
                  app._gyro_status()[0] == "OFFLINE")

    def _recovery_live(self):
        return f"gyro={self.app._gyro_status()[0]}  serial={'OK' if self.app.ser else 'NO'}"

    def _recovery_done(self, _actual):
        app = self.app
        app.gyro_link = True
        app._handle_line(self._full_data(
            72, 24.86, 58, 31.2, 3284, 15, -8, 997,
            96, 0, 8, -3, 16, 6.5, -3.0))
        self._rec("异常恢复", "恢复链路后回到 LIVE",
                  app._gyro_status()[0] == "LIVE",
                  app._gyro_status())
        self._rec("异常恢复", "恢复后数据继续更新",
                  abs(app.gyro_target_roll - 6.5) < 1e-6
                  and abs(app.gyro_target_pitch + 3.0) < 1e-6,
                  (app.gyro_target_roll, app.gyro_target_pitch))

    # ---------- 收尾 ----------
    def _finish(self):
        self.finished = True
        ok = self.c.fail == 0
        self.report_path, self.json_path = self._write_reports()
        app = self.app
        app._set_status(
            f"测试完成 · {self.c.ok} 通过 / {self.c.fail} 失败",
            "ok" if ok else "error")
        app._notify("系统测试完成" if ok else "系统测试存在失败项",
                    hm.ACCENT if ok else hm.DANGER, 5000)
        self._log(f"测试完成：{self.c.ok} 通过 / {self.c.fail} 失败")
        self.hud.log.configure(state="normal")
        self.hud.log.insert(
            "end", f"==== TEST COMPLETE · {self.c.ok} PASS / {self.c.fail} FAIL ====\n")
        self.hud.log.see("end")
        self.hud.log.configure(state="disabled")
        self.hud_shot = RESULT_DIR / "system_test_hud.png"
        try:
            capture.capture_widget(self.hud.win, self.hud_shot)
        except Exception:  # noqa: BLE001
            self.hud_shot = None
        print(f"系统测试完成：{self.c.ok} 通过 / {self.c.fail} 失败")
        print(f"报告：{self.report_path}")
        print(f"JSON：{self.json_path}")
        if self.hold:
            return
        self.root.after(1800, self.root.destroy)

    def _write_reports(self):
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        modules = []
        for record in self.c.records:
            if record["module"] not in modules:
                modules.append(record["module"])

        lines = [
            "# 系统功能自动化测试报告",
            "",
            f"- 生成时间：{now.strftime('%Y-%m-%d %H:%M:%S %z')}",
            f"- 总体结果：**{'通过' if self.c.fail == 0 else '存在失败'}**",
            f"- 断言：{self.c.ok} 通过 / {self.c.fail} 失败",
            f"- 环境：{platform.system()} {platform.release()}，"
            f"Python {platform.python_version()}，Tk UI 缩放 {self.app.ui_scale:.3f}",
            f"- 虚拟串口：COM5 HC-05（无真实硬件）",
            "",
            "## 模块覆盖与结论",
            "",
            "| 功能模块 | 用例数 | 通过 | 失败 | 结论 |",
            "|---|---:|---:|---:|---|",
        ]
        for module in modules:
            recs = [r for r in self.c.records if r["module"] == module]
            passed = sum(r["passed"] for r in recs)
            failed = len(recs) - passed
            lines.append(
                f"| {module} | {len(recs)} | {passed} | {failed} | "
                f"{'通过' if failed == 0 else '失败'} |")

        lines += [
            "",
            "## 前台测试台证据",
            "",
            "![系统测试台](../test-results/system_test_hud.png)",
            "",
            "主界面在每个长时阶段同步显示实时数据与测试状态；三张阶段截图如下：",
            "",
            "- [陀螺仪阶段](../test-results/system_gyro.png)",
            "- [温度曲线阶段](../test-results/system_temperature.png)",
            "- [雷达阶段](../test-results/system_radar.png)",
            "",
            "## 长时可视化验证",
            "",
            "| 模块 | 要求 | 实际展示 | 数据量 | 状态/结果 | 证据 |",
            "|---|---:|---:|---|---|---|",
        ]
        for module in LONG_STAGES:
            stats = self.long_stats.get(module, {})
            detail = []
            if "states" in stats:
                detail.append("状态 " + "/".join(stats["states"]))
            if "points" in stats:
                detail.append(f"曲线 {stats['points']} 点")
            if "angles" in stats:
                detail.append(f"角度 {stats['angles']} 种")
            lines.append(
                f"| {module} | ≥5.0s | {stats.get('duration', 0):.2f}s | "
                f"{stats.get('frames', 0)} 帧 | {'，'.join(detail)} | "
                f"[截图](../{stats.get('shot', '')}) |")

        lines += [
            "",
            "## 分类用例明细",
            "",
        ]
        for module in modules:
            recs = [r for r in self.c.records if r["module"] == module]
            lines += [
                f"### {module}",
                "",
                "| 用例 | 结果 | 观测值 |",
                "|---|---|---|",
            ]
            for rec in recs:
                detail = rec["detail"].replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| {rec['case']} | {'PASS' if rec['passed'] else 'FAIL'} | "
                    f"{detail} |")
            lines.append("")

        lines += [
            "## 覆盖范围",
            "",
            "- 界面与 DPI：主窗口、三栏、画布、缩放因子、控件边界。",
            "- 协议与通信：PING/GET、CRLF、HEX、PONG、版本和错误应答日志。",
            "- 控制：舵机角度、锁定、周期遥测、雷达开关。",
            "- 数据：`+DATA` 全字段、6/11 字段 `$D`、边界值、NaN、坏帧。",
            "- 展示：电脑状态圆环、传感器标签、历史缓存、PC 帧。",
            "- 长时：3D 姿态仪、温度曲线、超声波雷达各至少 5 秒。",
            "- 异常恢复：超时 HOLD、非法帧 DATA ERR、断连 OFFLINE、恢复 LIVE。",
            "",
            "## 测试边界",
            "",
            "- 串口传输由 FakeSerial 模拟，验证协议层和 UI 响应；真实蓝牙配对、",
            "  CH340 驱动、线缆与供电质量需在目标硬件上复测。",
            "- 电脑性能数据由固定虚拟快照提供；真实 PDH、DWM、HWiNFO 读数",
            "  受机器环境影响，状态提示逻辑已覆盖。",
            "- 长时测试使用真实 Tk 事件循环和 30fps 动画，但时间为自动化调度，",
            "  不替代老化测试。",
            "",
            "## 虚拟数据概况",
            "",
            f"- 陀螺仪：0.05s 间隔注入正弦姿态，"
            f"{self.long_stats.get('陀螺仪', {}).get('frames', 0)} 帧；覆盖 "
            + "/".join(self.long_stats.get('陀螺仪', {}).get('states', [])) + "。",
            f"- 温度：0.4s 间隔注入 "
            f"{self.long_stats.get('温度曲线', {}).get('range_c', [0, 0])[0]:.2f}–"
            f"{self.long_stats.get('温度曲线', {}).get('range_c', [0, 0])[1]:.2f}℃，"
            f"{self.long_stats.get('温度曲线', {}).get('frames', 0)} 帧；包含格式错误帧。",
            f"- 雷达：0.05s 间隔扫描 "
            f"{self.long_stats.get('超声波雷达', {}).get('frames', 0)} 帧、"
            f"{self.long_stats.get('超声波雷达', {}).get('angles', 0)} 种角度；"
            "包含 -1 无回波、250cm 超量程和 999° 越界。",
            "- 基础用例另覆盖：0/180° 舵机、±2000mg 加速度、±90° 俯仰、"
            "NaN、旧 6 字段帧。",
            "",
            "## 可复现命令",
            "",
            "```bat",
            "python tools/system_test.py",
            "python tools/system_test.py --hold",
            "python selftest.py",
            "```",
            "",
            "主测试脚本使用固定随机序列和固定时间表，不依赖真实串口或本机性能数据。",
        ]
        report_path = REPORT_DIR / "system_test_report.md"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        data = {
            "generated_at": now.isoformat(),
            "result": "PASS" if self.c.fail == 0 else "FAIL",
            "passed": self.c.ok,
            "failed": self.c.fail,
            "unit_regression": self.unit_result,
            "long_stages": self.long_stats,
            "hud_evidence": "test-results/system_test_hud.png",
            "stages": self.stage_records,
            "cases": self.c.records,
        }
        json_path = RESULT_DIR / "system_test_results.json"
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return report_path, json_path


def run_unit_tests():
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "selftest.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        text = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"结果:\s*(\d+)\s*通过\s*/\s*(\d+)\s*失败", text)
        if m:
            ok, fail = int(m.group(1)), int(m.group(2))
            summary = f"{ok} 通过 / {fail} 失败"
            passed = proc.returncode == 0 and fail == 0
        else:
            summary = f"无法解析输出，退出码 {proc.returncode}"
            passed = False
        return {"passed": passed, "summary": summary, "output": text[-2000:]}
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "summary": f"执行失败：{exc}", "output": ""}


def main():
    parser = argparse.ArgumentParser(description="STM32 上位机系统测试")
    parser.add_argument("--hold", action="store_true", help="测试结束后保持窗口")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("该测试需要 Windows 桌面环境")

    unit_result = run_unit_tests()
    hm.enable_dpi_awareness()
    hm._ui_scale_for = lambda root, work_w, work_h: 1.25
    if hm.sysinfo is not None:
        hm.sysinfo.Collector = DemoCollector

    root = tk.Tk()
    app = hm.MonitorApp(root)
    root.title("STM32 Host · 系统功能测试")
    app.sys = DemoCollector()
    app.ser = FakeSerial()
    app.port_box["values"] = ("COM5  HC-05 (SPP)",)
    app.port_var.set("COM5  HC-05 (SPP)")
    app.baud_var.set("115200")
    app.active_baud = 115200
    app.gyro_link = True
    app.connect_btn.configure(text="断开连接")
    app._set_status("自动化测试准备中", "warn")
    root.update_idletasks()
    root.update()

    harness = Harness(root, app, unit_result)
    harness.hold = args.hold
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.after(250, harness.start)
    root.mainloop()


if __name__ == "__main__":
    main()
