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

import math
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

try:
    import sysinfo          # 本机性能采集（纯 ctypes，无第三方依赖）
except Exception:           # 采集模块不可用不影响串口功能
    sysinfo = None

BAUD_CHOICES = ["9600", "19200", "38400", "57600", "115200"]
DEFAULT_BAUD = "9600"
HISTORY = 120
FLOAT_KEYS = ("temp", "chip", "roll", "pitch")
RADAR_MAX_CM = 200          # 雷达视图量程，超过的点压在边缘
RADAR_DPS = 60              # 雷达扫描角速度，与固件默认一致
RADAR_TRAIL = 14            # 扫描线拖尾的条数

BG = "#1b1b1f"
PANEL = "#26262b"
FG = "#e6e6e6"
MUTED = "#9a9aa2"
ACCENT = "#5dcaa5"
WARN = "#ef9f27"


def _fmt_speed(kbs):
    """KB/s 转紧凑显示。"""
    try:
        kbs = float(kbs)
    except (TypeError, ValueError):
        return "--"
    if kbs >= 1024.0:
        return "%.1fM" % (kbs / 1024.0)
    return "%.0fK" % kbs


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
        self.radar_on = False       # 雷达模式是否开启
        self.radar_angle = 0        # 最近一次收到的云台角度
        self.radar_disp = 0.0       # 显示角度（浮点，做本地补间）
        self.radar_prev = 0.0       # 上一帧显示角度，用来判断扫描方向
        self.radar_last_dist = -1   # 最近一次收到的距离，-1 表示无回波
        self.radar_pts = {}         # 角度(0-180) -> 距离(cm)

        # ---- 电脑状态采集：程序一打开就开始，不依赖串口 ----
        self.sys = None
        self.pc_last = 0.0          # 上次推送电脑数据的时间
        self.sys_last = 0.0         # 上次重绘圆环的时间
        self.pc_err_noted = False   # 是否已提示过"固件不认识 PC 命令"
        if sysinfo is not None:
            try:
                self.sys = sysinfo.Collector(interval=1.0)
                self.sys.start()
            except Exception:
                self.sys = None

        self._build_ui()
        self._draw_gyro()
        self._draw_radar()
        self.root.after(50, self._poll)
        self.root.after(33, self._animate)

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
        # clam 主题自带的复选框指示器画的是一个"叉"，换成自己画的方框 + 对勾
        self._install_dark_check(style)

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

        # ---- 左栏（可滚动）----
        # 内容高度超过窗口，用 Canvas + 内嵌 Frame 做垂直滚动，鼠标滚轮控制
        scroll_host = ttk.Frame(body)
        scroll_host.pack(side="left", fill="both", expand=True)

        self.scroll = tk.Canvas(scroll_host, bg=BG, highlightthickness=0,
                                yscrollincrement=24)
        vbar = ttk.Scrollbar(scroll_host, orient="vertical", command=self.scroll.yview)
        self.scroll.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.scroll.pack(side="left", fill="both", expand=True)

        left = ttk.Frame(self.scroll)
        self._left_win = self.scroll.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>",
                  lambda e: self.scroll.configure(scrollregion=self.scroll.bbox("all")))
        self.scroll.bind("<Configure>",
                         lambda e: self.scroll.itemconfigure(self._left_win, width=e.width))

        # ---- 电脑状态（圆环）：程序打开即开始刷新，与串口是否连接无关 ----
        ttk.Label(left, text="电脑状态", style="Muted.TLabel").pack(anchor="w", pady=(2, 2))
        self.sys_canvas = tk.Canvas(left, height=148, bg=PANEL, highlightthickness=0)
        self.sys_canvas.pack(fill="x")
        self.sys_canvas.bind("<Configure>", lambda e: self._draw_sysinfo())

        grid = ttk.Frame(left)
        grid.pack(fill="x", pady=(0, 6))
        self.value_labels = {}
        fields = [
            ("光照", "light", "%"), ("温度", "temp", "℃"), ("湿度", "humi", "%"),
            ("片内温度", "chip", "℃"), ("供电", "vdd", "mV"),
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

        # 两块画布给同一个高度，左右顶边才会严格对齐（之前 Notebook 的标签栏
        # 会把温度曲线整体压下去，和右边的陀螺仪错开二十多像素）
        chart_h = 180
        charts = ttk.Frame(left)
        charts.pack(fill="x")

        self.canvas = tk.Canvas(charts, height=chart_h, bg=PANEL, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        gyro_col = ttk.Frame(charts, padding=(12, 0, 0, 0))
        gyro_col.pack(side="right", fill="y")
        self.gyro = tk.Canvas(gyro_col, width=300, height=chart_h, bg=PANEL,
                              highlightthickness=0)
        self.gyro.pack(fill="both", expand=True)
        self.gyro.bind("<Configure>", lambda e: self._draw_gyro())

        # 雷达图单独占一块，放在温度曲线下方，靠滚轮往下滚才能看到
        ttk.Label(left, text="雷达", style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
        self.radar = tk.Canvas(left, height=260, bg=PANEL, highlightthickness=0)
        self.radar.pack(fill="x")
        self.radar.bind("<Configure>", lambda e: self._draw_radar())
        self.root.bind_all("<MouseWheel>", self._on_wheel)

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

        self.radar_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text=f"雷达模式 {RADAR_DPS}°/s", variable=self.radar_var,
                        command=self._send_radar).pack(anchor="w", pady=1)

        self.pcdata_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="发送电脑数据 1s", variable=self.pcdata_var,
                        command=self._toggle_pcdata).pack(anchor="w", pady=1)
        ttk.Label(right, text="把电脑状态每秒推给 STM32",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(0, 0))

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

        self.ts_var = tk.BooleanVar(value=True)
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

    def _install_dark_check(self, style):
        """把 ttk.Checkbutton 的指示器换成自绘的深色方框 + 对勾。

        clam 主题默认用一条交叉线表示"选中"，在深色界面上看起来就是个叉，
        容易误读成"未选中/错误"。这里直接生成两张 14x14 的位图做指示器。
        """
        def box(color):
            img = tk.PhotoImage(width=14, height=14)
            img.put(PANEL, to=(0, 0, 14, 14))
            for i in range(14):
                img.put(color, (i, 0))
                img.put(color, (i, 13))
                img.put(color, (0, i))
                img.put(color, (13, i))
            return img

        off = box("#4a4a52")
        on = box(ACCENT)
        for x, y in [(2, 7), (3, 8), (4, 9), (5, 10), (6, 9), (7, 8),
                     (8, 7), (9, 6), (10, 5), (11, 4)]:
            on.put(ACCENT, (x, y))
            on.put(ACCENT, (x, y - 1))

        self._ck_images = (off, on)   # 必须留引用，否则图片会被回收
        try:
            style.element_create("Dark.Check.indicator", "image", off,
                                 ("selected", on), border=0, sticky="")
        except tk.TclError:
            return                     # 已经创建过（多实例场景）
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Dark.Check.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])

    def _on_wheel(self, event):
        """鼠标滚轮只在指针位于左栏时滚动左栏，避免和日志框抢事件。"""
        c = self.scroll
        if not c.winfo_ismapped():
            return
        px, py = c.winfo_pointerxy()
        if (c.winfo_rootx() <= px <= c.winfo_rootx() + c.winfo_width()
                and c.winfo_rooty() <= py <= c.winfo_rooty() + c.winfo_height()):
            c.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _scroll_to_radar(self):
        """把左栏滚到底，让雷达图露出来。"""
        self.scroll.update_idletasks()
        self.scroll.yview_moveto(1.0)

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

    def _send_radar(self):
        if self.radar_var.get():
            self.radar_pts.clear()
            self.radar_angle = 0
            self.radar_disp = 0.0
            self.radar_prev = 0.0
            self.radar_last_dist = -1
            self.radar_on = True
            self._scroll_to_radar()      # 滚到底，让雷达图露出来
            self._send(f"RADAR,1,{RADAR_DPS}")
        else:
            self.radar_on = False
            self._send("RADAR,0")
        self._draw_radar()

    # ---------- 电脑数据 ----------
    def _toggle_pcdata(self):
        if self.pcdata_var.get():
            self.pc_last = 0.0
            self.pc_err_noted = False
            if self.ser is None:
                self._log("! 未连接：连上后会自动开始推送电脑数据")
            else:
                self._log("> 开始推送电脑数据（1s 一帧）")
        else:
            self._log("> 已停止推送电脑数据")

    def _pc_payload(self):
        """拼一帧电脑数据，全整数，MCU 端用 atoi/sscanf 就能解，不用解析浮点。

        PC,cpu=42,mem=63,gpu=21,ct=42,gt=49,cf=4000,gc=608,
           ru=102,rt=160,dn=806,up=41,fps=75

        ct / gt / gc 为 -1 表示这台机器读不到（没装 HWiNFO）
        ru / rt      单位 0.1GB（102 表示 10.2GB）
        dn / up      单位 KB/s
        """
        d = self.sys.snapshot() if self.sys is not None else None
        if d is None:
            return "PC,cpu=0,mem=0,gpu=-1,ct=-1,gt=-1,cf=-1,gc=-1,ru=0,rt=0,dn=0,up=0,fps=-1"

        def i(v):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return -1

        return ("PC,cpu=%d,mem=%d,gpu=%d,ct=%d,gt=%d,cf=%d,gc=%d,"
                "ru=%d,rt=%d,dn=%d,up=%d,fps=%d") % (
                    i(d["cpu"]), i(d["mem"]), i(d["gpu"]),
                    i(d["cpu_temp"]), i(d["gpu_temp"]),
                    i(d["cpu_mhz"]), i(d["gpu_mhz"]),
                    i(d["ram_used"] * 10), i(d["ram_total"] * 10),
                    i(d["net_down"]), i(d["net_up"]), i(d["fps"]))

    def _draw_sysinfo(self):
        """五个圆环：CPU / GPU / RAM / NET / FPS。"""
        c = self.sys_canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 150 or h < 80:
            return

        d = self.sys.snapshot() if self.sys is not None else None
        if d is None:
            c.create_text(w / 2, h / 2, text="采集模块不可用",
                          fill=MUTED, font=("Consolas", 10))
            return

        def clamp(v):
            return max(0.0, min(100.0, float(v)))

        cpu, mem, gpu = d["cpu"], d["mem"], d["gpu"]

        def sub_of(*parts):
            s = " ".join(p for p in parts if p)
            return s if s else "--"

        cpu_sub = sub_of("%d°C" % d["cpu_temp"] if d["cpu_temp"] >= 0 else "",
                         "%.2fG" % (d["cpu_mhz"] / 1000.0) if d["cpu_mhz"] > 0 else "")
        gpu_sub = sub_of("%d°C" % d["gpu_temp"] if d["gpu_temp"] >= 0 else "",
                         "%dM" % d["gpu_mhz"] if d["gpu_mhz"] > 0 else "")

        net_total = d["net_down"] + d["net_up"]
        # 网速用对数刻度，线性刻度下几 KB/s 在环上几乎看不见
        net_ratio = math.log10(1 + net_total) / math.log10(1 + 10240) if net_total > 0 else 0.0

        hz = d["hz"] if d["hz"] > 0 else 60.0
        fps_ratio = clamp(d["fps"] / hz * 100.0) / 100.0 if d["fps"] >= 0 else 0.0

        items = [
            ("CPU", clamp(cpu) / 100.0, "%d%%" % cpu, cpu_sub, "#4ecdc4", 13),
            ("GPU", clamp(gpu) / 100.0 if gpu >= 0 else 0.0,
             "%d%%" % gpu if gpu >= 0 else "--", gpu_sub, "#ffa94d", 13),
            ("RAM", clamp(mem) / 100.0, "%d%%" % mem,
             "%.1f/%.0fG" % (d["ram_used"], d["ram_total"]), "#a78bfa", 13),
            ("NET", min(1.0, net_ratio), "↓" + _fmt_speed(d["net_down"]),
             "↑" + _fmt_speed(d["net_up"]) + "/s", "#6ee7b7", 11),
            ("FPS", min(1.0, fps_ratio), "%d" % d["fps"] if d["fps"] >= 0 else "--",
             "%dHz" % d["hz"] if d["hz"] > 0 else "--", "#e6e6e6", 15),
        ]

        slot = w / float(len(items))
        r = 38.0
        if h < 140:
            r = max(22.0, (h - 46) / 2.0)
        cy = 14 + 10 + r
        sub_y = cy + r + 14

        for idx, (name, ratio, center, sub, color, fsize) in enumerate(items):
            cx = slot * (idx + 0.5)
            box = (cx - r, cy - r, cx + r, cy + r)

            c.create_text(cx, 14, text=name, fill=MUTED, font=("Consolas", 9))

            # 底槽
            c.create_oval(box[0], box[1], box[2], box[3],
                          outline="#3a3a42", width=7)

            # 进度弧：从 12 点方向顺时针
            if ratio > 0.004:
                c.create_arc(box[0], box[1], box[2], box[3],
                             start=90, extent=-ratio * 359.9,
                             style="arc", outline=color, width=7)

            c.create_text(cx, cy, text=center, fill=FG,
                          font=("Consolas", fsize, "bold"))
            c.create_text(cx, sub_y, text=sub, fill=MUTED, font=("Consolas", 8))

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

        # 电脑状态圆环：1s 重绘一次，跟采集节奏一致
        if time.time() - self.sys_last >= 1.0:
            self.sys_last = time.time()
            self._draw_sysinfo()

        # 电脑数据推送：勾选后每 1s 一帧。
        # 用 _send_bytes 且不传 label，避免每秒往日志里写一行把有用信息冲掉
        if self.pcdata_var.get() and self.ser is not None:
            if time.time() - self.pc_last >= 1.0:
                self.pc_last = time.time()
                payload = (self._pc_payload() + "\r\n").encode("ascii", "ignore")
                self._send_bytes(payload)

        self.root.after(50, self._poll)

    def _handle_line(self, line):
        if not line:
            return

        # 老固件不认识 PC 命令，每秒会回一条 -ERR,UNKNOWN。
        # 推送期间把这些回复吞掉，否则日志会被刷屏；只在第一次提示一句。
        if line == "-ERR,UNKNOWN" and self.pcdata_var.get():
            if not self.pc_err_noted:
                self.pc_err_noted = True
                self._log("! 固件暂不支持 PC 命令（回 -ERR,UNKNOWN），已自动忽略这些回复")
            return

        # $R 是 20Hz 的雷达帧，全写日志会把有用信息冲掉
        if not line.startswith("$R,"):
            self._log(f"< {line}")

        if line.startswith("+PONG"):
            self.await_pong = 0.0
            self.status.configure(text=f"在线 @ {self.active_baud}", style="Muted.TLabel")

        if line.startswith("+OK,BAUD") and self.pending_baud:
            target = self.pending_baud
            self.pending_baud = 0
            self._log(f"设备已切到 {target}，正在重连 ...")
            self.root.after(250, self._reconnect_at_target)

        if line.startswith("+OK,RADAR="):
            on = line.endswith("1")
            self.radar_on = on
            self.radar_var.set(on)
            if on:
                self._scroll_to_radar()
            self._draw_radar()

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
        elif line.startswith("$R,"):
            parts = line[3:].split(",")
            if len(parts) >= 2:
                try:
                    ang = int(parts[0])
                    dist = int(parts[1])
                except ValueError:
                    return
                self.radar_angle = max(0, min(180, ang))
                self.radar_last_dist = dist
                if dist >= 0:
                    self.radar_pts[self.radar_angle] = dist
                if not self.radar_on:
                    self.radar_on = True
                    self.radar_var.set(True)
                    self.radar_disp = float(self.radar_angle)
                    self.radar_prev = float(self.radar_angle)
                    self._scroll_to_radar()
                self._draw_radar()
        elif line.startswith("-ERR"):
            self._log(f"! 设备返回错误 {line}")

    def _refresh_values(self):
        d = self.data
        self.value_labels["light"].configure(text=str(d["light"]))
        self.value_labels["temp"].configure(text=f"{d['temp']:.2f}")
        self.value_labels["humi"].configure(text=str(d["humi"]))
        self.value_labels["chip"].configure(text=f"{d['chip']:.1f}")
        self.value_labels["vdd"].configure(text=str(d["vdd"]))
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

    @staticmethod
    def _blend(c_from, c_to, t):
        """两个 #rrggbb 颜色按 t（0~1）线性插值。Tk 画布没有透明度，用这个模拟渐隐。"""
        t = max(0.0, min(1.0, t))
        a = [int(c_from[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c_to[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    def _animate(self):
        """雷达扫描的本地补间。

        固件按 20Hz 回 $R，如果只在收到帧时才重画，扫描线是"跳"的。
        这里以 30fps 把显示角度逐步追向最新角度，视觉上才是连续转动。
        """
        if self.radar_on:
            diff = self.radar_angle - self.radar_disp
            if abs(diff) > 0.15:
                self.radar_prev = self.radar_disp
                self.radar_disp += diff * 0.45
                self._draw_radar()
        self.root.after(33, self._animate)

    def _draw_radar(self):
        """半圆雷达：0° 在左、90° 朝上、180° 在右。

        扫描线由 _animate() 补间到 30fps，身后留一段渐隐拖尾。
        """
        c = self.radar
        c.delete("all")
        w = max(c.winfo_width(), 220)
        h = max(c.winfo_height(), 110)

        # 半径要给刻度标签留位置：标签画在 r+10 处，90° 那个会顶到画布上沿
        r = min(h - 40, (w - 64) / 2)
        if r < 30:
            c.create_text(w / 2, h / 2, text="视图太小", fill=MUTED)
            return
        cx = w / 2
        cy = h - 18

        # 距离环
        for frac in (0.25, 0.5, 0.75, 1.0):
            rr = r * frac
            c.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=0, extent=180,
                         style="arc", outline="#33333a")

        # 角度刻度线
        for a in range(0, 181, 30):
            th = math.radians(a)
            c.create_line(cx, cy, cx - r * math.cos(th), cy - r * math.sin(th),
                          fill="#2c2c33")
            c.create_text(cx - (r + 10) * math.cos(th), cy - (r + 10) * math.sin(th),
                          text=str(a), fill="#55555e", font=("Consolas", 8))

        # 回波点
        for ang, dist in self.radar_pts.items():
            rr = r * min(dist / float(RADAR_MAX_CM), 1.0)
            th = math.radians(ang)
            x = cx - rr * math.cos(th)
            y = cy - rr * math.sin(th)
            color = ACCENT if dist < RADAR_MAX_CM * 0.6 else WARN
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

        # 扫描线拖尾：越靠后越接近背景色
        sign = 1.0 if self.radar_disp >= self.radar_prev else -1.0
        for k in range(RADAR_TRAIL, 0, -1):
            a = self.radar_disp - sign * k * 1.6
            if not 0 <= a <= 180:
                continue
            th = math.radians(a)
            c.create_line(cx, cy, cx - r * math.cos(th), cy - r * math.sin(th),
                          fill=self._blend(PANEL, ACCENT, 1.0 - k / float(RADAR_TRAIL + 1)))

        # 当前扫描线
        th = math.radians(self.radar_disp)
        c.create_line(cx, cy, cx - r * math.cos(th), cy - r * math.sin(th),
                      fill=ACCENT, width=2)
        c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=ACCENT, outline="")

        # 读数
        c.create_text(8, 10, text=f"角度 {self.radar_disp:5.1f}°", fill=FG, anchor="w",
                      font=("Microsoft YaHei UI", 10))
        c.create_text(w - 8, 10, text=f"量程 {RADAR_MAX_CM}cm", fill=MUTED, anchor="e",
                      font=("Consolas", 8))
        dist_txt = f"{self.radar_last_dist} cm" if self.radar_last_dist >= 0 else "---"
        c.create_text(8, h - 10, text=f"距离 {dist_txt}", fill=MUTED, anchor="w",
                      font=("Microsoft YaHei UI", 9))

        if not self.radar_pts:
            c.create_text(cx, cy - r * 0.55, text="等待回波…", fill="#55555e")

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
