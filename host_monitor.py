#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BASH-STM32 蓝牙上位机

依赖：pyserial（pip install pyserial）
运行：双击 run_host.bat，或 python host_monitor.py

功能：
- 串口连接，波特率可切换（连接中切换会自动重连）
- 一键把设备波特率同步成当前选择（发送 BAUD 命令）
- 实时显示光照、温度、湿度、片内温度、供电电压、加速度
- 温度曲线（最近 120 个采样点）
- 舵机角度滑条、舵机锁定、周期遥测开关
- 串口助手常用项：自动追加回车、HEX 发送、时间戳、定时发送、命令历史、清空日志

协议见 README「蓝牙串口协议」一节。
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

BAUD_CHOICES = ["9600", "19200", "38400", "57600", "115200"]
DEFAULT_BAUD = "9600"
HISTORY = 120
FLOAT_KEYS = ("temp", "chip", "roll", "pitch")

BG = "#1b1b1f"
PANEL = "#26262b"
FG = "#e6e6e6"
MUTED = "#9a9aa2"
ACCENT = "#5dcaa5"
WARN = "#ef9f27"


class MonitorApp:
    def __init__(self, root):
        self.root = root
        self.ser = None
        self.reader = None
        self.alive = False
        self.rx_queue = queue.Queue()
        self.history = []
        self.cmd_history = []
        self.cmd_index = 0
        self.data = {
            "light": 0, "temp": 0.0, "humi": 0, "chip": 0.0,
            "vdd": 0, "ax": 0, "ay": 0, "az": 0, "servo": 0, "lock": 0,
            "gx": 0, "gy": 0, "gz": 0, "roll": 0.0, "pitch": 0.0,
        }
        self.await_pong = 0.0
        self.timer_last = 0.0
        self.got_data = False       # 是否已收到过一帧数据
        self.active_baud = 0        # 当前端口实际使用的波特率
        self.pending_baud = 0       # 等待设备确认的目标波特率
        self.baud_deadline = 0.0
        self._build_ui()
        self._draw_gyro()
        self.root.after(50, self._poll)

    # ---------- 界面 ----------
    def _build_ui(self):
        self.root.title("BASH-STM32 蓝牙上位机")
        self.root.configure(bg=BG)
        self.root.geometry("960x730")
        self.root.minsize(880, 660)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=("Consolas", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Warn.TLabel", foreground=WARN)
        style.configure("Value.TLabel", foreground=ACCENT, font=("Consolas", 16, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 9))
        style.configure("Small.TButton", font=("Consolas", 9))
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.configure("TCombobox", fieldbackground=PANEL)
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG)

        # ---- 顶部：串口 + 波特率 ----
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text="串口").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=26, state="readonly")
        self.port_box.pack(side="left", padx=(6, 12))

        ttk.Button(top, text="刷新", width=6, command=self._refresh_ports).pack(side="left")

        ttk.Label(top, text="波特率").pack(side="left", padx=(16, 0))
        self.baud_var = tk.StringVar(value=DEFAULT_BAUD)
        self.baud_box = ttk.Combobox(top, textvariable=self.baud_var, values=BAUD_CHOICES,
                                     width=9, state="readonly")
        self.baud_box.pack(side="left", padx=6)
        self.baud_box.bind("<<ComboboxSelected>>", self._on_baud_change)

        self.connect_btn = ttk.Button(top, text="连接", width=8, command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=(10, 6))
        self.status = ttk.Label(top, text="未连接", style="Muted.TLabel")
        self.status.pack(side="left", padx=6)

        # ---- 主体 ----
        body = ttk.Frame(self.root, padding=(12, 0))
        body.pack(fill="both", expand=True)

        # 注意：右栏必须先 pack，否则左栏 expand=True 会把它挤成一条缝
        right = ttk.Frame(body, padding=(14, 0, 0, 0))
        right.pack(side="right", fill="y")

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        grid = ttk.Frame(left)
        grid.pack(fill="x", pady=(0, 6))
        self.value_labels = {}
        fields = [
            ("光照", "light", "%"), ("温度", "temp", "℃"), ("湿度", "humi", "%"),
            ("片内温度", "chip", "℃"), ("供电", "vdd", "mV"), ("舵机", "servo", "°"),
        ]
        for i, (name, key, unit) in enumerate(fields):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 26), pady=4)
            ttk.Label(cell, text=name, style="Muted.TLabel").pack(anchor="w")
            row = ttk.Frame(cell)
            row.pack(anchor="w")
            lbl = ttk.Label(row, text="--", style="Value.TLabel")
            lbl.pack(side="left")
            ttk.Label(row, text=unit, style="Muted.TLabel").pack(side="left", padx=(2, 0))
            self.value_labels[key] = lbl

        acc = ttk.Frame(left)
        acc.pack(fill="x", pady=(0, 6))
        ttk.Label(acc, text="加速度 (g)", style="Muted.TLabel").pack(anchor="w")
        self.acc_label = ttk.Label(acc, text="ax --   ay --   az --")
        self.acc_label.pack(anchor="w")

        ttk.Label(left, text="温度曲线 / 陀螺仪", style="Muted.TLabel").pack(anchor="w", pady=(6, 2))

        charts = ttk.Frame(left)
        charts.pack(fill="both", expand=True)

        chart_col = ttk.Frame(charts)
        chart_col.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(chart_col, height=160, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        gyro_col = ttk.Frame(charts, padding=(12, 0, 0, 0))
        gyro_col.pack(side="right", fill="y")
        self.gyro = tk.Canvas(gyro_col, width=300, height=160, bg=PANEL,
                              highlightthickness=0)
        self.gyro.pack(fill="both", expand=True)
        self.gyro.bind("<Configure>", lambda e: self._draw_gyro())

        # ---- 右侧控制 ----
        ttk.Label(right, text="舵机角度").pack(anchor="w")
        self.angle_label = ttk.Label(right, text="90°")
        self.angle_label.pack(anchor="w")
        self.scale = ttk.Scale(right, from_=0, to=180, orient="horizontal",
                               length=200, command=self._on_slider)
        self.scale.set(90)
        self.scale.pack(anchor="w", pady=(0, 2))
        ttk.Button(right, text="发送角度", command=self._send_servo).pack(anchor="w", pady=(0, 8))

        self.lock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="舵机锁定", variable=self.lock_var,
                        command=self._send_lock).pack(anchor="w", pady=1)

        self.stream_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="周期遥测 500ms", variable=self.stream_var,
                        command=self._send_stream).pack(anchor="w", pady=1)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)

        ttk.Label(right, text="快捷命令", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        quick = ttk.Frame(right)
        quick.pack(anchor="w")
        for i, cmd in enumerate(["PING", "GET", "SYS", "VER", "HELP", "RESET"]):
            ttk.Button(quick, text=cmd, width=7, style="Small.TButton",
                       command=lambda c=cmd: self._send(c)).grid(
                row=i // 2, column=i % 2, padx=2, pady=2)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(right, text="同步设备波特率", command=self._sync_device_baud).pack(anchor="w")
        ttk.Label(right, text="把设备切到当前波特率\n（掉电恢复 9600）",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(2, 0))

        # ---- 发送栏 ----
        send_bar = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        send_bar.pack(fill="x")

        ttk.Label(send_bar, text="发送").pack(side="left")
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(send_bar, textvariable=self.cmd_var, font=("Consolas", 10))
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.cmd_entry.bind("<Return>", lambda e: self._send_custom())
        self.cmd_entry.bind("<Up>", self._hist_prev)
        self.cmd_entry.bind("<Down>", self._hist_next)

        ttk.Button(send_bar, text="发送", width=6, command=self._send_custom).pack(side="left")
        ttk.Button(send_bar, text="清空日志", width=8,
                   command=self._clear_log).pack(side="left", padx=(6, 0))

        # ---- 选项栏 ----
        opts = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        opts.pack(fill="x")

        self.crlf_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="自动追加回车 (\\r\\n)", variable=self.crlf_var).pack(side="left")

        self.hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="HEX 发送", variable=self.hex_var).pack(side="left", padx=(14, 0))

        self.ts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="时间戳", variable=self.ts_var).pack(side="left", padx=(14, 0))

        self.timer_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="定时发送", variable=self.timer_var).pack(side="left", padx=(14, 0))
        self.period_var = tk.StringVar(value="1000")
        ttk.Entry(opts, textvariable=self.period_var, width=6).pack(side="left", padx=(4, 2))
        ttk.Label(opts, text="ms", style="Muted.TLabel").pack(side="left")

        # ---- 日志 ----
        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        log_frame.pack(fill="both")
        self.log = tk.Text(log_frame, height=8, bg=PANEL, fg=MUTED, bd=0,
                           font=("Consolas", 9), insertbackground=FG)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        # 串口枚举放在最后：它可能触发日志输出，此时所有控件必须已就绪
        self._refresh_ports()

    def _refresh_ports(self):
        if list_ports is None:
            self.port_box["values"] = []
            self._log("未安装 pyserial，请先执行：pip install pyserial")
            return
        ports = [f"{p.device}  {p.description}" for p in list_ports.comports()]
        self.port_box["values"] = ports
        if ports and not self.port_var.get():
            self.port_box.current(0)

    # ---------- 串口 ----------
    def _baud(self):
        try:
            return int(self.baud_var.get())
        except ValueError:
            return 9600

    def _on_baud_change(self, _event=None):
        # 连接中改波特率不能立刻重连：设备还在旧速率上，会直接失联。
        # 正确顺序是「先发 BAUD 命令让设备跟上，再重连」。
        if self.ser is not None and self._baud() != self.active_baud:
            self._log(f"波特率改为 {self._baud()}；"
                      f"点『同步设备波特率』让设备跟上，或断开后重连")

    def _toggle_connect(self):
        if self.ser is not None:
            self._disconnect()
            return
        if serial is None:
            messagebox.showerror("缺少依赖", "请先安装 pyserial：\n\npip install pyserial")
            return
        sel = self.port_var.get().strip()
        if not sel:
            messagebox.showwarning("提示", "请选择串口")
            return
        device = sel.split()[0]
        baud = self._baud()
        try:
            self.ser = serial.Serial(device, baud, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("连接失败", str(exc))
            self.ser = None
            return
        self.active_baud = baud
        self.alive = True
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        self.connect_btn.configure(text="断开")
        self.status.configure(text=f"已连接 {device} @ {baud}", style="Muted.TLabel")
        self._log(f"已连接 {device} @ {baud}")
        self.await_pong = time.time()
        self._send("PING")

    def _disconnect(self, silent=False):
        self.alive = False
        time.sleep(0.05)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
        self.ser = None
        self.active_baud = 0
        self.connect_btn.configure(text="连接")
        self.status.configure(text="未连接", style="Muted.TLabel")
        if not silent:
            self._log("已断开")

    def _read_loop(self):
        while self.alive and self.ser is not None:
            try:
                line = self.ser.readline()
            except Exception as exc:  # noqa: BLE001
                self.rx_queue.put(("ERR", str(exc)))
                break
            if line:
                self.rx_queue.put(("LINE", line.decode("utf-8", "replace").strip()))

    def _send_bytes(self, data, label=None):
        if self.ser is None:
            self._log("! 未连接，无法发送")
            return False
        try:
            self.ser.write(data)
            if label is not None:
                self._log(f"> {label}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"! 发送失败: {exc}")
            return False

    def _send(self, text):
        """发送一条命令；自动追加回车由勾选项决定。"""
        payload = text
        if self.crlf_var.get() and not payload.endswith("\r\n"):
            payload += "\r\n"
        return self._send_bytes(payload.encode("ascii", "ignore"), text)

    # ---------- 自定义发送 ----------
    def _parse_hex(self, text):
        cleaned = text.replace("0x", "").replace("0X", "")
        for sep in [",", ";", "-", "\t", "\n", " "]:
            cleaned = cleaned.replace(sep, " ")
        parts = [p for p in cleaned.split(" ") if p]
        try:
            return bytes(int(p, 16) for p in parts)
        except ValueError:
            return None

    def _send_custom(self):
        text = self.cmd_var.get()
        if not text:
            return
        if text not in self.cmd_history:
            self.cmd_history.append(text)
        self.cmd_index = len(self.cmd_history)

        if self.hex_var.get():
            data = self._parse_hex(text)
            if data is None:
                self._log(f"! 十六进制格式错误: {text}")
                return
            self._send_bytes(data, "[HEX] " + data.hex(" ").upper())
        else:
            self._send(text)

    def _hist_prev(self, _event):
        if not self.cmd_history:
            return
        self.cmd_index = max(0, self.cmd_index - 1)
        self.cmd_var.set(self.cmd_history[self.cmd_index])
        return "break"

    def _hist_next(self, _event):
        if not self.cmd_history:
            return
        self.cmd_index = min(len(self.cmd_history), self.cmd_index + 1)
        self.cmd_var.set("" if self.cmd_index >= len(self.cmd_history)
                         else self.cmd_history[self.cmd_index])
        return "break"

    def _sync_device_baud(self):
        """按「旧速率发命令 → 设备切换 → 主机用新速率重连」的顺序改波特率。"""
        if self.ser is None:
            messagebox.showwarning("提示", "请先连接")
            return
        target = self._baud()
        if target == self.active_baud:
            self._log(f"设备与主机都已是 {target}，无需同步")
            return
        self._send(f"BAUD,{target}")   # 此时链路仍在旧速率上，设备能收到
        self._log(f"已发送 BAUD,{target}，等待设备确认 ...")
        self.pending_baud = target
        self.baud_deadline = time.time() + 2.0

    def _reconnect_at_target(self):
        self._disconnect(silent=True)
        self.root.after(150, self._toggle_connect)

    # ---------- 控件回调 ----------
    def _on_slider(self, value):
        self.angle_label.configure(text=f"{int(float(value))}°")

    def _send_servo(self):
        angle = int(round(float(self.scale.get())))
        self._send(f"SERVO,{angle}")

    def _send_lock(self):
        self._send(f"LOCK,{1 if self.lock_var.get() else 0}")

    def _send_stream(self):
        self._send(f"STREAM,{1 if self.stream_var.get() else 0},500")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- 数据 ----------
    def _poll(self):
        try:
            while True:
                kind, payload = self.rx_queue.get_nowait()
                if kind == "LINE":
                    self._handle_line(payload)
                else:
                    self._log(f"! 串口错误: {payload}")
                    self._disconnect()
        except queue.Empty:
            pass

        # 连接后 1.5s 没收到 PONG，多半是波特率不对
        if self.await_pong and time.time() - self.await_pong > 1.5:
            self.await_pong = 0.0
            self._log("! 未收到 +PONG：检查设备是否上电、波特率是否一致")
            self.status.configure(text="已连接，无应答", style="Warn.TLabel")

        # 等设备确认 BAUD 超时：保持原速率，避免自己把链路弄断
        if self.pending_baud and time.time() > self.baud_deadline:
            self._log(f"! 设备未确认 BAUD，保持 {self.active_baud}")
            self.pending_baud = 0
            self.baud_var.set(str(self.active_baud))

        # 定时发送
        if self.timer_var.get() and self.ser is not None:
            try:
                period = max(50, int(self.period_var.get())) / 1000.0
            except ValueError:
                period = 1.0
            if time.time() - self.timer_last >= period:
                self.timer_last = time.time()
                text = self.cmd_var.get()
                if text:
                    if self.hex_var.get():
                        data = self._parse_hex(text)
                        if data:
                            self._send_bytes(data)
                    else:
                        self._send(text)

        self.root.after(50, self._poll)

    def _handle_line(self, line):
        if not line:
            return
        self._log(f"< {line}")

        if line.startswith("+PONG"):
            self.await_pong = 0.0
            self.status.configure(text=f"在线 @ {self.active_baud}", style="Muted.TLabel")

        if line.startswith("+OK,BAUD") and self.pending_baud:
            target = self.pending_baud
            self.pending_baud = 0
            self._log(f"设备已切到 {target}，正在重连 ...")
            self.root.after(250, self._reconnect_at_target)

        if line.startswith("+DATA,"):
            for item in line[6:].split(","):
                if "=" not in item:
                    continue
                key, _, val = item.partition("=")
                if key not in self.data:
                    continue
                try:
                    self.data[key] = float(val) if key in FLOAT_KEYS else int(float(val))
                except ValueError:
                    continue
            # 只有 +DATA 带 servo / lock，$D 不带，所以同步只在这里做，
            # 否则周期遥测会把用户刚勾选的锁定状态改回去。
            self.lock_var.set(bool(self.data["lock"]))
            self.scale.set(self.data["servo"])
            self.angle_label.configure(text=f"{self.data['servo']}°")
            self._refresh_values()
        elif line.startswith("$D,"):
            parts = line[3:].split(",")
            if len(parts) >= 6:
                try:
                    self.data["light"] = int(parts[0])
                    self.data["temp"] = int(parts[1]) / 100.0
                    self.data["humi"] = int(parts[2])
                    self.data["ax"] = int(parts[3])
                    self.data["ay"] = int(parts[4])
                    self.data["az"] = int(parts[5])
                    # 7~11 项为后加的陀螺仪与姿态，旧固件不会带
                    if len(parts) >= 11:
                        self.data["gx"] = int(parts[6])
                        self.data["gy"] = int(parts[7])
                        self.data["gz"] = int(parts[8])
                        self.data["roll"] = int(parts[9]) / 10.0
                        self.data["pitch"] = int(parts[10]) / 10.0
                except ValueError:
                    return
                self._refresh_values()
                self._push_history(self.data["temp"])
        elif line.startswith("-ERR"):
            self._log(f"! 设备返回错误 {line}")

    def _refresh_values(self):
        d = self.data
        self.value_labels["light"].configure(text=str(d["light"]))
        self.value_labels["temp"].configure(text=f"{d['temp']:.2f}")
        self.value_labels["humi"].configure(text=str(d["humi"]))
        self.value_labels["chip"].configure(text=f"{d['chip']:.1f}")
        self.value_labels["vdd"].configure(text=str(d["vdd"]))
        self.value_labels["servo"].configure(text=str(d["servo"]))
        self.acc_label.configure(
            text=f"ax {d['ax']/1000:.2f}   ay {d['ay']/1000:.2f}   az {d['az']/1000:.2f}")
        self.got_data = True
        self._draw_gyro()

    def _draw_gyro(self):
        """左侧姿态气泡（roll/pitch）+ 右侧三轴角速度条（Gx/Gy/Gz，±250°/s）。

        所有坐标按画布实际尺寸按比例计算，窗口缩放或高 DPI 下都不会越界。
        """
        c = self.gyro
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 110)

        if not self.got_data:
            c.create_text(w / 2, h / 2, text="等待陀螺仪数据…", fill=MUTED)
            return

        d = self.data
        mid_y = h * 0.44

        # ---- 姿态气泡 ----
        r = min(h * 0.30, w * 0.15)
        cx, cy = r + 10, mid_y
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#4a4a52", width=2)
        c.create_oval(cx - r * 0.55, cy - r * 0.55, cx + r * 0.55, cy + r * 0.55,
                      outline="#33333a")
        c.create_line(cx - r, cy, cx + r, cy, fill="#3a3a42")
        c.create_line(cx, cy - r, cx, cy + r, fill="#3a3a42")

        roll, pitch = d["roll"], d["pitch"]
        bx = cx + max(-1.0, min(1.0, roll / 45.0)) * r * 0.62
        by = cy - max(-1.0, min(1.0, pitch / 45.0)) * r * 0.62
        c.create_oval(bx - 7, by - 7, bx + 7, by + 7, fill=ACCENT, outline="")
        c.create_text(4, h - 10, text=f"R{roll:+.1f}  P{pitch:+.1f}",
                      fill=MUTED, anchor="w", font=("Consolas", 9))

        # ---- 三轴角速度条 ----
        x0 = w * 0.44
        x1 = w * 0.80
        mid = (x0 + x1) / 2
        span = x1 - x0
        spacing = max(min(66.0, (h - 80.0) / 2.0), 22.0)

        for i, (name, val) in enumerate((("Gx", d["gx"]), ("Gy", d["gy"]), ("Gz", d["gz"]))):
            y = mid_y + (i - 1) * spacing
            c.create_text(x0 - 7, y, text=name, fill=MUTED, anchor="e",
                          font=("Consolas", 9))
            c.create_line(x0, y, x1, y, fill="#3a3a42")
            c.create_line(mid, y - 7, mid, y + 7, fill="#55555e")
            frac = max(-1.0, min(1.0, val / 250.0))
            ex = mid + frac * span / 2
            mag = abs(val)
            color = ACCENT if mag < 20 else (WARN if mag < 150 else "#e24b4a")
            if abs(ex - mid) >= 1:
                c.create_line(mid, y, ex, y, fill=color, width=6)
            c.create_text(w - 5, y, text=f"{val:+d}", fill=FG, anchor="e",
                          font=("Consolas", 9))

    def _push_history(self, value):
        self.history.append(value)
        if len(self.history) > HISTORY:
            self.history.pop(0)
        self._draw_chart()

    def _draw_chart(self):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 100)
        if len(self.history) < 2:
            c.create_text(w // 2, h // 2, text="等待数据…", fill=MUTED)
            return
        lo = min(self.history)
        hi = max(self.history)
        if hi - lo < 1.0:
            hi = lo + 1.0
        step = w / (HISTORY - 1)
        pts = []
        for i, v in enumerate(self.history):
            x = i * step
            y = h - 12 - (v - lo) / (hi - lo) * (h - 28)
            pts.extend([x, y])
        c.create_line(*pts, fill=ACCENT, width=2, smooth=True)
        c.create_text(6, 8, text=f"{hi:.1f}", fill=MUTED, anchor="w")
        c.create_text(6, h - 8, text=f"{lo:.1f}", fill=MUTED, anchor="w")

    def _log(self, text):
        # 界面构建过程中可能就会打日志，此时 ts_var 还不存在
        if getattr(self, "ts_var", None) is not None and self.ts_var.get():
            text = time.strftime("%H:%M:%S ") + text
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 400:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_close(self):
        self._disconnect(silent=True)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
