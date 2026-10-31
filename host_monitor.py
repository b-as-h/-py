#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BASH-STM32 蓝牙上位机

依赖：pyserial（pip install pyserial）
运行：双击 run_host.bat，或 python host_monitor.py

功能：
- 串口连接，波特率可切换（连接中通过同步按钮安全切换）
- 一键把设备波特率同步成当前选择（发送 BAUD 命令）
- 实时显示光照、温度、湿度、片内温度、供电电压、加速度
- 温度曲线（最近 120 个采样点）
- 舵机角度滑条、舵机锁定、周期遥测开关
- 串口助手常用项：自动追加回车、HEX 发送、时间戳、定时发送、命令历史、清空日志

协议见 README「蓝牙串口协议」一节。
"""

import ctypes
import math
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, font as tkfont

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
GYRO_KEYS = ("gx", "gy", "gz", "roll", "pitch")
GYRO_STALE_S = 2.0          # 超过 2 秒无新姿态帧则标记 HOLD
GYRO_TAU_S = 0.10           # 30fps 下的姿态缓动时间常数

# 统一的桌面工具视觉规范：低亮度底色、分层面板、单一强调色。
BG = "#10151b"
PANEL = "#1a222b"
PANEL_ALT = "#222d38"
FG = "#edf3f7"
MUTED = "#93a4b3"
ACCENT = "#4fc3a1"
ACCENT_HOVER = "#6dd6b6"
WARN = "#f0ad4e"
DANGER = "#ef6a70"
INFO = "#65a9e8"
BORDER = "#31404d"
INPUT = "#121a22"
SHADOW = "#090d11"
FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# 3D 姿态仪：深色内嵌表面，低饱和蓝灰立方体与柔和三轴。
GYRO_BG = PANEL_ALT
GYRO_INK = "#d9e5ec"
GYRO_SUB = "#8294a5"
GYRO_LINE = BORDER
GYRO_CUBE_TOP = "#3d738b"
GYRO_CUBE_FRONT = "#2d6076"
GYRO_CUBE_RIGHT = "#244f63"
GYRO_CUBE_LEFT = "#29596f"
GYRO_CUBE_BACK = "#204757"
GYRO_CUBE_BOTTOM = "#1b3c4b"
GYRO_CUBE_EDGE = "#183743"
AXIS_X = "#c96a6e"
AXIS_Y = "#65a17c"
AXIS_Z = "#748fc8"
GYRO_LIVE = ACCENT
GYRO_HOLD = "#d3a15d"
GYRO_FAULT = "#cf7477"
UI_SCALE = 1.0


def _sp(value):
    """把 96 DPI 下的设计像素换算成当前界面像素。"""
    return max(1, int(round(value * UI_SCALE)))


def enable_dpi_awareness():
    """必须在 tk.Tk() 前调用，避免 Windows 对整窗做位图拉伸。"""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _work_area():
    """返回主屏工作区，避开任务栏；失败时回退到 Tk 屏幕尺寸。"""
    if sys.platform == "win32":
        rect = wintypes.RECT()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return 0, 0, 0, 0


def _ui_scale_for(root, work_w, work_h):
    """综合系统 DPI、屏幕尺寸和可用工作区，得到克制的 UI 缩放。"""
    dpi = float(root.winfo_fpixels("1i"))
    dpi_scale = dpi / 96.0
    work_w = work_w or root.winfo_screenwidth()
    work_h = work_h or root.winfo_screenheight()
    # 2K/1080 下让界面占到合适视觉面积；不是简单把所有元素放大 1.5 倍。
    preferred = max(1.0, min(1.40, work_w / 1900.0, work_h / 1080.0))
    fit_w = max(0.90, (work_w - 32) / 1100.0)
    fit_h = max(0.90, (work_h - 60) / 780.0)
    return max(0.90, min(max(dpi_scale, preferred), 1.50, fit_w, fit_h))


def _rounded_points(x1, y1, x2, y2, radius, steps=8):
    """生成圆角矩形顶点，供 Canvas 卡片使用。"""
    points = []
    for cx, cy, start in ((x2 - radius, y1 + radius, -90),
                          (x2 - radius, y2 - radius, 0),
                          (x1 + radius, y2 - radius, 90),
                          (x1 + radius, y1 + radius, 180)):
        for i in range(steps + 1):
            angle = math.radians(start + i * 90 / steps)
            points.extend((cx + radius * math.cos(angle),
                           cy + radius * math.sin(angle)))
    return points


class RoundedCard(tk.Canvas):
    """轻量圆角卡片；真实内容放在 body 中，不增加额外依赖。"""

    def __init__(self, master, radius=None, fill=PANEL, **kwargs):
        super().__init__(master, bg=BG, bd=0, highlightthickness=0, **kwargs)
        self.radius = radius if radius is not None else _sp(14)
        self.card_fill = fill
        self.body = tk.Frame(self, bg=fill)
        self._body_window = self.create_window(2, 2, window=self.body, anchor="nw")
        self.bind("<Configure>", self._draw_card)

    def _draw_card(self, _event=None):
        w, h = max(8, self.winfo_width()), max(8, self.winfo_height())
        r = min(self.radius, w // 3, h // 3)
        self.delete("card-shadow")
        self.delete("card-body")
        self.create_polygon(_rounded_points(3, _sp(4), w - 1, h - 1, r),
                            smooth=False, fill=SHADOW, outline="", tags="card-shadow")
        self.create_polygon(_rounded_points(1, 1, w - 3, h - _sp(4), r),
                            smooth=False, fill=self.card_fill, outline=BORDER,
                            width=1, tags="card-body")
        self.itemconfigure(self._body_window, width=w - 6, height=h - _sp(7))
        self.coords(self._body_window, 2, 2)
        self.tag_raise(self._body_window)


class ActionButton(tk.Button):
    """统一按钮的默认、悬停、按下、禁用和加载状态。"""

    VARIANTS = {
        "primary": (ACCENT, "#10231d", ACCENT_HOVER, "#10231d"),
        "secondary": (PANEL_ALT, FG, BORDER, FG),
        "ghost": (PANEL, MUTED, PANEL_ALT, FG),
        "danger": ("#48262b", "#ffd9dc", "#683039", "#ffffff"),
    }

    def __init__(self, master, variant="secondary", **kwargs):
        bg, fg, hover, active = self.VARIANTS[variant]
        kwargs.setdefault("bg", bg)
        kwargs.setdefault("fg", fg)
        kwargs.setdefault("activebackground", hover)
        kwargs.setdefault("activeforeground", active)
        kwargs.setdefault("disabledforeground", "#63717d")
        kwargs.setdefault("font", (FONT_UI, 10, "bold"))
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("padx", _sp(12))
        kwargs.setdefault("pady", _sp(6))
        kwargs.setdefault("cursor", "hand2")
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.variant = variant
        self._hover_bg = hover
        self._loading = False
        self._loading_text = ""
        self._normal_text = self.cget("text")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _event=None):
        if str(self.cget("state")) != "disabled" and not self._loading:
            self.configure(bg=self._hover_bg)

    def _on_leave(self, _event=None):
        if not self._loading:
            self.configure(bg=self.VARIANTS[self.variant][0])

    def set_loading(self, loading, text="处理中…"):
        self._loading = loading
        if loading:
            self._normal_text = self.cget("text")
            self.configure(text=text, bg=INPUT, fg=MUTED, cursor="watch")
        else:
            self.configure(text=self._normal_text,
                           bg=self.VARIANTS[self.variant][0],
                           fg=self.VARIANTS[self.variant][1], cursor="hand2")


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
        self.gyro_link = False      # 串口链路状态（用于姿态仪断连提示）
        self.gyro_has_data = False
        self.gyro_target_roll = 0.0
        self.gyro_target_pitch = 0.0
        self.gyro_disp_roll = 0.0
        self.gyro_disp_pitch = 0.0
        self.gyro_gx = 0.0
        self.gyro_gy = 0.0
        self.gyro_gz = 0.0
        self.gyro_last_rx = 0.0
        self.gyro_fault_until = 0.0
        self.gyro_anim_at = time.monotonic()
        self.gyro_drawn_state = ""
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
        global UI_SCALE
        self.root.title("STM32 Host · 桌面监控终端")
        self.root.configure(bg=BG)

        work_x, work_y, work_w, work_h = _work_area()
        if not work_w or not work_h:
            work_x, work_y = 0, 0
            work_w = self.root.winfo_screenwidth()
            work_h = self.root.winfo_screenheight()
        UI_SCALE = _ui_scale_for(self.root, work_w, work_h)
        self.root.tk.call("tk", "scaling", 96.0 * UI_SCALE / 72.0)
        self.ui_scale = UI_SCALE

        win_w = min(_sp(1100), work_w - _sp(24))
        win_h = min(_sp(780), work_h - _sp(40))
        pos_x = work_x + max(_sp(8), (work_w - win_w) // 2)
        pos_y = work_y + max(_sp(8), (work_h - win_h) // 2)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(min(_sp(980), work_w - _sp(24)),
                          min(_sp(700), work_h - _sp(40)))

        # 统一 Tk/ttk 字体，避免不同控件回退到不同系统字体。
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=FONT_UI, size=10)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=FG,
                        font=(FONT_UI, 10))
        style.configure("Muted.TLabel", foreground=MUTED, font=(FONT_UI, 10))
        style.configure("Warn.TLabel", foreground=WARN)
        style.configure("Success.TLabel", foreground=ACCENT)
        style.configure("Danger.TLabel", foreground=DANGER)
        style.configure("Section.TLabel", foreground=FG, font=(FONT_UI, 11, "bold"))
        style.configure("Title.TLabel", foreground=FG, font=(FONT_UI, 15, "bold"))
        style.configure("Value.TLabel", background=PANEL_ALT, foreground=ACCENT,
                        font=(FONT_MONO, 15, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=FG,
                        font=(FONT_UI, 10), padding=(0, _sp(2)))
        style.map("TCheckbutton", background=[("active", PANEL)],
                  foreground=[("disabled", "#63717d"), ("active", ACCENT_HOVER)])
        style.configure("TCombobox", fieldbackground=INPUT, background=PANEL_ALT,
                        foreground=FG, arrowcolor=MUTED, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER,
                        padding=_sp(5))
        style.map("TCombobox", fieldbackground=[("readonly", INPUT)],
                  foreground=[("readonly", FG)],
                  bordercolor=[("focus", ACCENT)],
                  arrowcolor=[("active", ACCENT)])
        style.configure("TEntry", fieldbackground=INPUT, foreground=FG,
                        insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=_sp(6))
        style.map("TEntry", bordercolor=[("focus", ACCENT)])
        style.configure("Horizontal.TScale", background=PANEL, troughcolor=INPUT,
                        bordercolor=INPUT, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TSeparator", background=BORDER)
        style.configure("Vertical.TScrollbar", background=PANEL_ALT,
                        troughcolor=PANEL, bordercolor=PANEL, arrowcolor=MUTED)
        self._install_dark_check(style)

        outer = tk.Frame(self.root, bg=BG, padx=_sp(16), pady=_sp(14))
        outer.pack(fill="both", expand=True)

        # ---- 顶部：产品标识、连接状态和串口参数 ----
        top_card = RoundedCard(outer, height=_sp(104))
        self.top_card = top_card
        top_card.pack(fill="x", pady=(0, _sp(12)))
        top = tk.Frame(top_card.body, bg=PANEL, padx=_sp(14), pady=_sp(10))
        top.pack(fill="both", expand=True)
        header = tk.Frame(top, bg=PANEL)
        header.pack(fill="x", pady=(0, _sp(8)))
        ttk.Label(header, text="STM32 HOST", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="桌面监控与串口调试终端",
                  style="Muted.TLabel").pack(side="left", padx=(_sp(12), 0),
                                               pady=(_sp(4), 0))
        self.status = ttk.Label(header, text="未连接", style="Muted.TLabel")
        self.status.pack(side="right", pady=(_sp(4), 0))
        dot = _sp(10)
        self.status_dot = tk.Canvas(header, width=dot, height=dot, bg=PANEL,
                                    highlightthickness=0)
        self.status_dot.pack(side="right", padx=(_sp(10), _sp(6)),
                             pady=(_sp(7), 0))
        self._status_circle = self.status_dot.create_oval(
            1, 1, dot - 1, dot - 1, fill=MUTED, outline="")
        self.toast = tk.Label(header, text="", bg=PANEL_ALT, fg=ACCENT,
                              font=(FONT_UI, 10), padx=_sp(8), pady=_sp(3))
        self.toast.pack(side="right", padx=(_sp(4), _sp(14)), pady=(_sp(2), 0))

        controls = tk.Frame(top, bg=PANEL)
        controls.pack(fill="x")
        ttk.Label(controls, text="串口", style="Muted.TLabel").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(controls, textvariable=self.port_var,
                                     width=31, state="readonly")
        self.port_box.pack(side="left", padx=(_sp(7), _sp(8)))
        self.refresh_btn = ActionButton(controls, text="刷新", variant="secondary",
                                        command=self._refresh_ports)
        self.refresh_btn.pack(side="left")
        ttk.Label(controls, text="波特率", style="Muted.TLabel").pack(
            side="left", padx=(_sp(20), 0))
        self.baud_var = tk.StringVar(value=DEFAULT_BAUD)
        self.baud_box = ttk.Combobox(controls, textvariable=self.baud_var,
                                     values=BAUD_CHOICES,
                                     width=10, state="readonly")
        self.baud_box.pack(side="left", padx=(_sp(7), _sp(8)))
        self.baud_box.bind("<<ComboboxSelected>>", self._on_baud_change)
        self.connect_btn = ActionButton(controls, text="连接设备", width=12,
                                        variant="primary", command=self._toggle_connect)
        self.connect_btn.pack(side="right")
        ActionButton(controls, text="同步设备速率", width=13, variant="secondary",
                     command=self._sync_device_baud).pack(side="right", padx=(0, _sp(8)))

        # ---- 主体：右侧控制固定宽度，左侧监控区自适应 ----
        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)
        right_card = RoundedCard(body, width=_sp(244))
        self.right_card = right_card
        right_card.pack(side="right", fill="y", padx=(_sp(12), 0))
        right = tk.Frame(right_card.body, bg=PANEL, padx=_sp(16), pady=_sp(10))
        self.right_panel = right
        right.pack(fill="both", expand=True)

        scroll_host = RoundedCard(body)
        scroll_host.pack(side="left", fill="both", expand=True)
        scroll_box = tk.Frame(scroll_host.body, bg=PANEL)
        scroll_box.pack(fill="both", expand=True)
        self.scroll = tk.Canvas(scroll_box, bg=PANEL, highlightthickness=0,
                                yscrollincrement=_sp(24))
        vbar = ttk.Scrollbar(scroll_box, orient="vertical", command=self.scroll.yview)
        self.scroll.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y", padx=(0, _sp(2)), pady=_sp(2))
        self.scroll.pack(side="left", fill="both", expand=True)

        left = tk.Frame(self.scroll, bg=PANEL, padx=_sp(14), pady=_sp(12))
        self._left_win = self.scroll.create_window((0, 0), window=left, anchor="nw")
        left.bind("<Configure>",
                  lambda e: self.scroll.configure(scrollregion=self.scroll.bbox("all")))
        self.scroll.bind("<Configure>",
                         lambda e: self.scroll.itemconfigure(self._left_win, width=e.width))

        # ---- 电脑状态（圆环）：程序打开即开始刷新，与串口是否连接无关 ----
        sys_head = tk.Frame(left, bg=PANEL)
        sys_head.pack(fill="x", pady=(0, _sp(2)))
        ttk.Label(sys_head, text="电脑性能", style="Section.TLabel").pack(side="left")
        tk.Label(sys_head, text="LIVE · 1s", bg=PANEL, fg=ACCENT,
                 font=(FONT_MONO, 10)).pack(side="right", pady=(_sp(3), 0))
        self.sys_canvas = tk.Canvas(left, height=_sp(148), bg=PANEL,
                                    highlightthickness=0)
        self.sys_canvas.pack(fill="x")
        self.sys_canvas.bind("<Configure>", lambda e: self._draw_sysinfo())

        data_head = tk.Frame(left, bg=PANEL)
        data_head.pack(fill="x", pady=(_sp(4), _sp(4)))
        ttk.Label(data_head, text="传感器数据", style="Section.TLabel").pack(side="left")
        ttk.Label(data_head, text="设备实时快照", style="Muted.TLabel").pack(
            side="right", pady=(_sp(3), 0))
        grid = tk.Frame(left, bg=PANEL)
        grid.pack(fill="x", pady=(0, _sp(8)))
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="sensor")
        self.value_labels = {}
        fields = [
            ("光照", "light", "%"), ("温度", "temp", "℃"), ("湿度", "humi", "%"),
            ("片内温度", "chip", "℃"), ("供电", "vdd", "mV"),
        ]
        for i, (name, key, unit) in enumerate(fields):
            cell = tk.Frame(grid, bg=PANEL_ALT, padx=_sp(10), pady=_sp(8))
            cell.grid(row=i // 3, column=i % 3, sticky="nsew",
                      padx=_sp(4), pady=_sp(4))
            tk.Label(cell, text=name, bg=PANEL_ALT, fg=MUTED,
                     font=(FONT_UI, 10)).pack(anchor="w")
            row = tk.Frame(cell, bg=PANEL_ALT)
            row.pack(fill="x", pady=(_sp(3), 0))
            lbl = ttk.Label(row, text="--", style="Value.TLabel", width=7, anchor="w")
            lbl.pack(side="left")
            tk.Label(row, text=unit, bg=PANEL_ALT, fg=MUTED,
                     font=(FONT_UI, 10)).pack(side="left", padx=(_sp(4), 0),
                                               pady=(_sp(4), 0))
            self.value_labels[key] = lbl

        acc = tk.Frame(left, bg=PANEL_ALT, padx=_sp(10), pady=_sp(8))
        acc.pack(fill="x", pady=(_sp(4), _sp(10)))
        tk.Label(acc, text="三轴加速度", bg=PANEL_ALT, fg=MUTED,
                 font=(FONT_UI, 10)).pack(side="left")
        self.acc_label = tk.Label(acc, text="ax --   ay --   az --   g", bg=PANEL_ALT,
                                  fg=FG, font=(FONT_MONO, 11, "bold"))
        self.acc_label.pack(side="right")

        chart_head = tk.Frame(left, bg=PANEL)
        self.chart_head = chart_head
        chart_head.pack(fill="x", pady=(_sp(4), _sp(4)))
        ttk.Label(chart_head, text="温度与 IMU 姿态", style="Section.TLabel").pack(side="left")
        ttk.Label(chart_head, text="实时曲线 / 3D 姿态",
                  style="Muted.TLabel").pack(side="right", pady=(_sp(3), 0))

        # 两块画布给同一个高度，左右顶边才会严格对齐（之前 Notebook 的标签栏
        # 会把温度曲线整体压下去，和右边的陀螺仪错开二十多像素）
        chart_h = _sp(220)
        charts = ttk.Frame(left)
        charts.pack(fill="x")

        self.canvas = tk.Canvas(charts, height=chart_h, bg=PANEL_ALT,
                                highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        gyro_col = ttk.Frame(charts, padding=(_sp(10), 0, 0, 0))
        gyro_col.pack(side="right", fill="y")
        self.gyro = tk.Canvas(gyro_col, width=_sp(320), height=chart_h,
                              bg=GYRO_BG, highlightthickness=0)
        self.gyro.pack(fill="both", expand=True)
        self.gyro.bind("<Configure>", lambda e: self._draw_gyro())

        # 雷达图单独占一块，放在温度曲线下方，靠滚轮往下滚才能看到
        radar_head = tk.Frame(left, bg=PANEL)
        self.radar_head = radar_head
        radar_head.pack(fill="x", pady=(_sp(12), _sp(4)))
        ttk.Label(radar_head, text="雷达扫描", style="Section.TLabel").pack(side="left")
        ttk.Label(radar_head, text=f"0—180°  ·  {RADAR_MAX_CM} cm",
                  style="Muted.TLabel").pack(side="right", pady=(_sp(3), 0))
        self.radar = tk.Canvas(left, height=_sp(260), bg=PANEL_ALT,
                               highlightthickness=0)
        self.radar.pack(fill="x", pady=(0, _sp(4)))
        self.radar.bind("<Configure>", lambda e: self._draw_radar())
        self.root.bind_all("<MouseWheel>", self._on_wheel)

        # ---- 右侧控制 ----
        ttk.Label(right, text="设备控制", style="Section.TLabel").pack(
            anchor="w", pady=(0, _sp(7)))
        ttk.Label(right, text="舵机角度", style="Muted.TLabel").pack(anchor="w")
        self.angle_label = tk.Label(right, text="90°", bg=PANEL, fg=FG,
                                    font=(FONT_MONO, 21, "bold"))
        self.angle_label.pack(anchor="w")
        self.scale = ttk.Scale(right, from_=0, to=180, orient="horizontal",
                               length=_sp(200), command=self._on_slider)
        self.scale.set(90)
        self.scale.pack(fill="x", pady=(0, _sp(5)))
        ActionButton(right, text="发送角度", variant="primary", pady=_sp(4),
                     command=self._send_servo).pack(fill="x", pady=(0, _sp(7)))

        self.lock_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="舵机锁定", variable=self.lock_var,
                        command=self._send_lock).pack(anchor="w", fill="x")

        self.stream_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="周期遥测  500 ms", variable=self.stream_var,
                        command=self._send_stream).pack(anchor="w", fill="x")

        self.radar_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text=f"雷达模式  {RADAR_DPS}°/s", variable=self.radar_var,
                        command=self._send_radar).pack(anchor="w", fill="x")

        self.pcdata_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="发送电脑数据  1s", variable=self.pcdata_var,
                        command=self._toggle_pcdata).pack(anchor="w", fill="x")

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=_sp(8))

        ttk.Label(right, text="快捷命令", style="Muted.TLabel").pack(
            anchor="w", pady=(0, _sp(3)))
        quick = tk.Frame(right, bg=PANEL)
        quick.pack(fill="x")
        for col in range(3):
            quick.columnconfigure(col, weight=1, uniform="quick")
        for i, cmd in enumerate(["PING", "GET", "SYS", "VER", "HELP", "RESET"]):
            ActionButton(quick, text=cmd, width=5, padx=_sp(5), pady=_sp(4),
                         variant="danger" if cmd == "RESET" else "secondary",
                         command=lambda c=cmd: self._send(c)).grid(
                row=i // 3, column=i % 3, padx=_sp(3), pady=_sp(3), sticky="ew")

        # ---- 底部：串口命令与日志终端 ----
        bottom_card = RoundedCard(outer, height=_sp(214))
        self.bottom_card = bottom_card
        bottom_card.pack(fill="x", pady=(_sp(12), 0))
        bottom = tk.Frame(bottom_card.body, bg=PANEL, padx=_sp(14), pady=_sp(10))
        bottom.pack(fill="both", expand=True)
        log_head = tk.Frame(bottom, bg=PANEL)
        log_head.pack(fill="x", pady=(0, _sp(7)))
        ttk.Label(log_head, text="串口终端", style="Section.TLabel").pack(side="left")
        ttk.Label(log_head, text="> 发送   < 接收   ! 异常",
                  style="Muted.TLabel").pack(side="right")

        send_bar = tk.Frame(bottom, bg=PANEL)
        send_bar.pack(fill="x", pady=(0, _sp(6)))
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(send_bar, textvariable=self.cmd_var,
                                   font=(FONT_MONO, 10))
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, _sp(8)))
        self.cmd_entry.bind("<Return>", lambda e: self._send_custom())
        self.cmd_entry.bind("<Up>", self._hist_prev)
        self.cmd_entry.bind("<Down>", self._hist_next)
        ActionButton(send_bar, text="发送命令", variant="primary",
                     command=self._send_custom).pack(side="left")
        ActionButton(send_bar, text="清空", variant="secondary",
                     command=self._clear_log).pack(side="left", padx=(_sp(8), 0))

        opts = tk.Frame(bottom, bg=PANEL)
        opts.pack(fill="x", pady=(0, _sp(7)))
        self.crlf_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="追加 CRLF", variable=self.crlf_var).pack(side="left")
        self.hex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="HEX 发送", variable=self.hex_var).pack(
            side="left", padx=(_sp(12), 0))
        self.ts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="时间戳", variable=self.ts_var).pack(
            side="left", padx=(_sp(12), 0))
        self.timer_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="定时发送", variable=self.timer_var).pack(
            side="left", padx=(_sp(12), 0))
        self.period_var = tk.StringVar(value="1000")
        ttk.Entry(opts, textvariable=self.period_var, width=6).pack(
            side="left", padx=(_sp(3), _sp(3)))
        ttk.Label(opts, text="ms", style="Muted.TLabel").pack(side="left")
        ttk.Label(opts, text="↑ / ↓ 历史命令", style="Muted.TLabel").pack(side="right")

        log_frame = tk.Frame(bottom, bg=BORDER, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=6, bg=INPUT, fg="#b8c7d3", bd=0,
                           font=(FONT_MONO, 10), insertbackground=FG,
                           selectbackground=BORDER, selectforeground=FG,
                           padx=_sp(9), pady=_sp(7),
                           spacing1=_sp(1), spacing3=_sp(1),
                           yscrollcommand=lambda *args: log_scroll.set(*args))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.configure(state="disabled")

        # 串口枚举放在最后：它可能触发日志输出，此时所有控件必须已就绪
        self._refresh_ports()

    def _set_status(self, text, tone="muted"):
        """同步状态文字和状态灯，避免多处的视觉状态不一致。"""
        styles = {"muted": "Muted.TLabel", "warn": "Warn.TLabel",
                  "ok": "Success.TLabel", "error": "Danger.TLabel"}
        colors = {"muted": MUTED, "warn": WARN, "ok": ACCENT, "error": DANGER}
        self.status.configure(text=text, style=styles.get(tone, "Muted.TLabel"))
        self.status_dot.itemconfigure(self._status_circle,
                                      fill=colors.get(tone, MUTED))

    def _notify(self, text, tone=ACCENT, duration=1400):
        """轻量操作反馈；新消息替换旧消息，不堆积定时器和动画。"""
        old_job = getattr(self, "_toast_job", None)
        if old_job is not None:
            try:
                self.root.after_cancel(old_job)
            except tk.TclError:
                pass
        self.toast.configure(text=text, fg=tone)
        self._toast_job = self.root.after(duration, lambda: self.toast.configure(text=""))

    def _install_dark_check(self, style):
        """把 ttk.Checkbutton 指示器换成随 DPI 重绘的方框 + 对勾。"""
        size = _sp(14)
        border = max(1, round(size / 14))

        def box(color):
            img = tk.PhotoImage(width=size, height=size)
            img.put(PANEL, to=(0, 0, size, size))
            for i in range(size):
                for b in range(border):
                    img.put(color, (i, b))
                    img.put(color, (i, size - 1 - b))
                    img.put(color, (b, i))
                    img.put(color, (size - 1 - b, i))
            return img

        off = box("#4a4a52")
        on = box(ACCENT)
        scale = size / 14.0
        for x, y in [(2, 7), (3, 8), (4, 9), (5, 10), (6, 9), (7, 8),
                     (8, 7), (9, 6), (10, 5), (11, 4)]:
            px, py = round(x * scale), round(y * scale)
            dot = max(1, border)
            on.put(ACCENT, to=(px, py, min(size, px + dot), min(size, py + dot)))
            on.put(ACCENT, to=(px, max(0, py - dot),
                               min(size, px + dot), py))

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
        self.gyro_link = True
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        self.connect_btn.configure(text="等待应答")
        self._set_status(f"已连接 {device} @ {baud} · 等待设备", "warn")
        self._log(f"已连接 {device} @ {baud}")
        self._draw_gyro()
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
        self.gyro_link = False
        self.connect_btn.configure(text="连接设备")
        self._set_status("未连接", "muted")
        self._draw_gyro()
        if not silent:
            self._log("已断开")
            self._notify("连接已断开", MUTED)

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
            self._notify("当前未连接设备", WARN)
            return False
        try:
            self.ser.write(data)
            if label is not None:
                self._log(f"> {label}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"! 发送失败: {exc}")
            self._notify("发送失败", DANGER)
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
        if w < _sp(150) or h < _sp(80):
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
            ("CPU", clamp(cpu) / 100.0, "%d%%" % cpu, cpu_sub, True, 13),
            ("GPU", clamp(gpu) / 100.0 if gpu >= 0 else 0.0,
             "%d%%" % gpu if gpu >= 0 else "--", gpu_sub, gpu >= 0, 13),
            ("RAM", clamp(mem) / 100.0, "%d%%" % mem,
             "%.1f/%.0fG" % (d["ram_used"], d["ram_total"]), True, 13),
            ("NET", min(1.0, net_ratio), "↓" + _fmt_speed(d["net_down"]),
             "↑" + _fmt_speed(d["net_up"]) + "/s", True, 11),
            ("FPS", min(1.0, fps_ratio), "%d" % d["fps"] if d["fps"] >= 0 else "--",
             "%dHz" % d["hz"] if d["hz"] > 0 else "--", d["fps"] >= 0, 15),
        ]

        slot = w / float(len(items))
        r = float(_sp(38))
        if h < _sp(140):
            r = max(float(_sp(22)), (h - _sp(46)) / 2.0)
        cy = _sp(14) + _sp(10) + r
        sub_y = cy + r + _sp(14)
        ring_width = max(2, _sp(7))

        for idx, (name, ratio, center, sub, available, fsize) in enumerate(items):
            cx = slot * (idx + 0.5)
            box = (cx - r, cy - r, cx + r, cy + r)
            if not available:
                color = "#4a5967"
            elif name == "FPS":
                # FPS 越高越好，不应该按负载阈值变红。
                color = ACCENT
            elif ratio >= 0.88:
                color = DANGER
            elif ratio >= 0.72:
                color = WARN
            else:
                color = ACCENT

            c.create_text(cx, _sp(14), text=name, fill=MUTED,
                          font=("Consolas", 9))

            # 底槽
            c.create_oval(box[0], box[1], box[2], box[3],
                          outline=BORDER, width=ring_width)

            # 进度弧：从 12 点方向顺时针
            if ratio > 0.004:
                c.create_arc(box[0], box[1], box[2], box[3],
                             start=90, extent=-ratio * 359.9,
                             style="arc", outline=color, width=ring_width)

            c.create_text(cx, cy, text=center,
                          fill=FG if available else MUTED,
                          font=("Consolas", fsize, "bold"))
            c.create_text(cx, sub_y, text=sub, fill=MUTED,
                          font=("Consolas", 9))

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
            self._set_status("已连接，无应答", "warn")
            self._notify("设备未响应，请检查连接", WARN)

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

        if line.startswith("+OK"):
            self._notify("设备已确认", ACCENT)
        elif line.startswith("-ERR"):
            self._notify("命令执行失败", DANGER)

        if line.startswith("+PONG"):
            self.await_pong = 0.0
            self.connect_btn.configure(text="断开连接")
            self._set_status(f"在线 @ {self.active_baud}", "ok")
            self._notify("设备连接成功", ACCENT)

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
            gyro_values = {}
            gyro_bad = False
            for item in line[6:].split(","):
                if "=" not in item:
                    continue
                key, _, val = item.partition("=")
                if key not in self.data:
                    continue
                try:
                    raw = float(val)
                except ValueError:
                    if key in GYRO_KEYS:
                        gyro_bad = True
                    continue
                if not math.isfinite(raw):
                    if key in GYRO_KEYS:
                        gyro_bad = True
                    continue
                self.data[key] = raw if key in FLOAT_KEYS else int(raw)
                if key in GYRO_KEYS:
                    gyro_values[key] = raw
            # 只有 +DATA 带 servo / lock，$D 不带，所以同步只在这里做，
            # 否则周期遥测会把用户刚勾选的锁定状态改回去。
            self.lock_var.set(bool(self.data["lock"]))
            self.scale.set(self.data["servo"])
            self.angle_label.configure(text=f"{self.data['servo']}°")
            self._refresh_values()
            if "roll" in gyro_values and "pitch" in gyro_values:
                self._set_gyro_target(
                    gyro_values.get("gx", self.gyro_gx),
                    gyro_values.get("gy", self.gyro_gy),
                    gyro_values.get("gz", self.gyro_gz),
                    gyro_values["roll"], gyro_values["pitch"])
            elif gyro_bad:
                self._mark_gyro_fault()
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
                if len(parts) >= 11:
                    self._set_gyro_target(
                        self.data["gx"], self.data["gy"], self.data["gz"],
                        self.data["roll"], self.data["pitch"])
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

    @staticmethod
    def _shortest_angle(current, target):
        """角度插值走最短方向，避免 -179° → +179° 时绕一整圈。"""
        return (target - current + 180.0) % 360.0 - 180.0

    @staticmethod
    def _rotate_xyz(x, y, z, roll, pitch):
        """先绕 X 轴俯仰，再绕 Y 轴横滚。"""
        cp, sp = math.cos(pitch), math.sin(pitch)
        cr, sr = math.cos(roll), math.sin(roll)
        y1 = y * cp - z * sp
        z1 = y * sp + z * cp
        return x * cr + z1 * sr, y1, -x * sr + z1 * cr

    @staticmethod
    def _project_xyz(x, y, z, cx, cy, scale):
        """等距投影：X 向右、Y 向左前、Z 向上。"""
        return cx + scale * (1.08 * x - 0.48 * y), cy + scale * (-z + 0.34 * y)

    def _mark_gyro_fault(self):
        self.gyro_fault_until = time.monotonic() + 1.2
        self._draw_gyro()

    def _set_gyro_target(self, gx, gy, gz, roll, pitch):
        """接收一帧已解析姿态。非有限值不进入动画，保留上一姿态。"""
        try:
            gx, gy, gz = float(gx), float(gy), float(gz)
            roll, pitch = float(roll), float(pitch)
        except (TypeError, ValueError):
            self._mark_gyro_fault()
            return False
        if not all(math.isfinite(v) for v in (gx, gy, gz, roll, pitch)):
            self._mark_gyro_fault()
            return False

        self.gyro_gx = max(-2000.0, min(2000.0, gx))
        self.gyro_gy = max(-2000.0, min(2000.0, gy))
        self.gyro_gz = max(-2000.0, min(2000.0, gz))
        self.gyro_target_roll = (roll + 180.0) % 360.0 - 180.0
        self.gyro_target_pitch = max(-90.0, min(90.0, pitch))
        self.gyro_last_rx = time.monotonic()
        self.gyro_fault_until = 0.0

        first = not self.gyro_has_data
        self.gyro_has_data = True
        self.got_data = True
        if first:
            # 第一帧直接对齐，避免从上电初始 0° 大幅甩入。
            self.gyro_disp_roll = self.gyro_target_roll
            self.gyro_disp_pitch = self.gyro_target_pitch
        self._draw_gyro()
        return True

    def _gyro_status(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.gyro_link:
            return "OFFLINE", GYRO_SUB, "链路已断开 · 保留最后姿态"
        if now < self.gyro_fault_until:
            return "DATA ERR", GYRO_FAULT, "数据异常 · 保持上一姿态"
        if not self.gyro_has_data:
            return "WAIT", GYRO_SUB, "等待 IMU 数据"
        if now - self.gyro_last_rx > GYRO_STALE_S:
            return "HOLD", GYRO_HOLD, "数据暂停 · 等待新帧"
        return "LIVE", GYRO_LIVE, ""

    def _step_gyro(self, now=None):
        """30fps 姿态缓动；静止时不重绘，避免持续占用 Canvas。"""
        now = time.monotonic() if now is None else now
        state = self._gyro_status(now)[0]
        state_changed = state != self.gyro_drawn_state

        if not self.gyro_link or not self.gyro_has_data:
            self.gyro_anim_at = now
            if state_changed:
                self._draw_gyro(now)
            return False

        dt = max(0.0, min(0.10, now - self.gyro_anim_at))
        self.gyro_anim_at = now
        if dt <= 0:
            if state_changed:
                self._draw_gyro(now)
            return False

        alpha = 1.0 - math.exp(-dt / GYRO_TAU_S)
        dr = self._shortest_angle(self.gyro_disp_roll, self.gyro_target_roll)
        dp = self.gyro_target_pitch - self.gyro_disp_pitch
        if abs(dr) < 0.03:
            dr = 0.0
        if abs(dp) < 0.03:
            dp = 0.0
        moved = abs(dr) > 0.001 or abs(dp) > 0.001
        if moved:
            self.gyro_disp_roll += dr * alpha
            self.gyro_disp_pitch += dp * alpha
        if state_changed or moved:
            self._draw_gyro(now)
        return moved

    def _draw_gyro(self, now=None):
        """白底 3D 姿态仪：蓝色立方体 + 红/绿/蓝三轴。"""
        now = time.monotonic() if now is None else now
        c = self.gyro
        c.delete("all")
        w = max(c.winfo_width(), _sp(220))
        h = max(c.winfo_height(), _sp(150))
        state, state_color, state_msg = self._gyro_status(now)
        self.gyro_drawn_state = state
        has_data = self.gyro_has_data

        c.create_rectangle(1, 1, w - 1, h - 1, fill=GYRO_BG, outline=GYRO_LINE)
        c.create_text(_sp(12), _sp(12), text="3D ATTITUDE", anchor="nw",
                      fill=GYRO_SUB, font=(FONT_MONO, 8))
        dot = max(3, _sp(4))
        dx, dy = w - _sp(86), _sp(17)
        c.create_oval(dx - dot, dy - dot, dx + dot, dy + dot,
                      fill=state_color, outline="")
        c.create_text(w - _sp(12), _sp(12), text=state, anchor="ne",
                      fill=state_color, font=(FONT_MONO, 9, "bold"))

        hud_top = h - _sp(52)
        c.create_line(_sp(12), hud_top, w - _sp(12), hud_top, fill=GYRO_LINE)
        draw_bottom = hud_top - _sp(22)
        if state_msg:
            c.create_text(w / 2, draw_bottom + _sp(4), text=state_msg,
                          fill=state_color, font=(FONT_UI, 8))
        cx = w / 2
        cy = (_sp(34) + draw_bottom) / 2
        scale = max(_sp(28), min(w * 0.21,
                                  (draw_bottom - _sp(34)) / 3.5))
        fade = (not has_data) or (not self.gyro_link)

        def shade(color):
            return self._blend(color, GYRO_BG, 0.74) if fade else color

        roll = math.radians(self.gyro_disp_roll)
        pitch = math.radians(self.gyro_disp_pitch)
        # 投影射线与 _project_xyz 的剪切变换严格对应；按射线深度从后往前
        # 绘制六个面，避免错误背面裁剪造成缺口或尖角。
        ray = (1.08, 2.25, 0.765)

        def transform(x, y, z):
            return self._rotate_xyz(x, y, z, roll, pitch)

        def project(x, y, z):
            tx, ty, tz = transform(x, y, z)
            return self._project_xyz(tx, ty, tz, cx, cy, scale), (tx, ty, tz)

        # 负向轴尾先画，立方体后画；正向箭头最后叠加，保证三轴从中心清晰穿出。
        origin, _ = project(0.0, 0.0, 0.0)
        for color, neg in ((AXIS_X, (-1.55, 0.0, 0.0)),
                           (AXIS_Z, (0.0, 0.0, -1.62))):
            p1, _ = project(*neg)
            c.create_line(p1[0], p1[1], origin[0], origin[1],
                          fill=shade(color), width=max(1, _sp(2)),
                          capstyle="round")

        verts = (
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        )
        faces = (
            ((0, 1, 2, 3), GYRO_CUBE_BOTTOM),
            ((4, 5, 6, 7), GYRO_CUBE_TOP),
            ((0, 1, 5, 4), GYRO_CUBE_FRONT),
            ((2, 3, 7, 6), GYRO_CUBE_BACK),
            ((1, 2, 6, 5), GYRO_CUBE_RIGHT),
            ((3, 0, 4, 7), GYRO_CUBE_LEFT),
        )
        visible = []
        for idxs, color in faces:
            points = []
            depth = 0.0
            for idx in idxs:
                point, world = project(*verts[idx])
                points.extend(point)
                depth += sum(world[i] * ray[i] for i in range(3)) / len(idxs)
            visible.append((depth, points, color))
        visible.sort(key=lambda item: item[0], reverse=True)
        for _, points, color in visible:
            c.create_polygon(points, fill=shade(color), outline=shade(GYRO_CUBE_EDGE),
                             width=1)

        axes = (
            (AXIS_X, (2.25, 0.0, 0.0)),
            (AXIS_Y, (0.0, 2.25, 0.0)),
            (AXIS_Z, (0.0, 0.0, 2.12)),
        )
        arrow = (max(8, _sp(11)), max(10, _sp(15)), max(3, _sp(5)))
        for color, pos in axes:
            p2, _ = project(*pos)
            c.create_line(origin[0], origin[1], p2[0], p2[1],
                          fill=shade(color), width=max(1, _sp(2)),
                          arrow="last", arrowshape=arrow)

        # 底部 HUD：姿态与三轴角速度使用同一组轴色。
        val_color = GYRO_INK if state in ("LIVE", "HOLD") else GYRO_SUB
        y1 = hud_top + _sp(12)
        y2 = hud_top + _sp(34)
        roll_txt = f"{self.gyro_disp_roll:+.1f}°" if has_data else "--"
        pitch_txt = f"{self.gyro_disp_pitch:+.1f}°" if has_data else "--"
        c.create_text(_sp(12), y1, text="ROLL", anchor="w",
                      fill=GYRO_SUB, font=(FONT_UI, 8))
        c.create_text(_sp(52), y1, text=roll_txt, anchor="w",
                      fill=GYRO_INK, font=(FONT_MONO, 10, "bold"))
        c.create_text(w / 2 + _sp(8), y1, text="PITCH", anchor="w",
                      fill=GYRO_SUB, font=(FONT_UI, 8))
        c.create_text(w / 2 + _sp(56), y1, text=pitch_txt, anchor="w",
                      fill=GYRO_INK, font=(FONT_MONO, 10, "bold"))

        x = _sp(12)
        c.create_text(x, y2, text="ω", anchor="w", fill=GYRO_SUB,
                      font=(FONT_MONO, 9))
        for label, color, value in (
                ("X", AXIS_X, self.gyro_gx),
                ("Y", AXIS_Y, self.gyro_gy),
                ("Z", AXIS_Z, self.gyro_gz)):
            x += _sp(21)
            c.create_text(x, y2, text=label, anchor="w", fill=color,
                          font=(FONT_MONO, 9, "bold"))
            x += _sp(13)
            text = f"{value:+.0f}" if has_data else "--"
            c.create_text(x, y2, text=text, anchor="w", fill=val_color,
                          font=(FONT_MONO, 9))
            x += _sp(46)
        c.create_text(w - _sp(12), y2, text="°/s", anchor="e",
                      fill=GYRO_SUB, font=(FONT_MONO, 8))

    @staticmethod
    def _blend(c_from, c_to, t):
        """两个 #rrggbb 颜色按 t（0~1）线性插值。Tk 画布没有透明度，用这个模拟渐隐。"""
        t = max(0.0, min(1.0, t))
        a = [int(c_from[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c_to[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    def _animate(self):
        """30fps 本地补间：雷达扫描 + 3D 姿态同步刷新。"""
        now = time.monotonic()
        if self.radar_on:
            diff = self.radar_angle - self.radar_disp
            if abs(diff) > 0.15:
                self.radar_prev = self.radar_disp
                self.radar_disp += diff * 0.45
                self._draw_radar()
        self._step_gyro(now)
        self.root.after(33, self._animate)

    def _draw_radar(self):
        """半圆雷达：0° 在左、90° 朝上、180° 在右。

        扫描线由 _animate() 补间到 30fps，身后留一段渐隐拖尾。
        """
        c = self.radar
        c.delete("all")
        w = max(c.winfo_width(), _sp(220))
        h = max(c.winfo_height(), _sp(110))

        # 半径要给刻度标签留位置：标签画在 r+10 处，90° 那个会顶到画布上沿
        r = min(h - _sp(40), (w - _sp(64)) / 2)
        if r < _sp(30):
            c.create_text(w / 2, h / 2, text="视图太小", fill=MUTED)
            return
        cx = w / 2
        cy = h - _sp(18)

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
            c.create_text(cx - (r + _sp(10)) * math.cos(th),
                          cy - (r + _sp(10)) * math.sin(th),
                          text=str(a), fill=MUTED, font=("Consolas", 9))

        # 回波点
        point_r = _sp(2)
        for ang, dist in self.radar_pts.items():
            rr = r * min(dist / float(RADAR_MAX_CM), 1.0)
            th = math.radians(ang)
            x = cx - rr * math.cos(th)
            y = cy - rr * math.sin(th)
            color = ACCENT if dist < RADAR_MAX_CM * 0.6 else WARN
            c.create_oval(x - point_r, y - point_r, x + point_r, y + point_r,
                          fill=color, outline="")

        # 扫描线拖尾：越靠后越接近背景色
        sign = 1.0 if self.radar_disp >= self.radar_prev else -1.0
        for k in range(RADAR_TRAIL, 0, -1):
            a = self.radar_disp - sign * k * 1.6
            if not 0 <= a <= 180:
                continue
            th = math.radians(a)
            c.create_line(cx, cy, cx - r * math.cos(th), cy - r * math.sin(th),
                          fill=self._blend(PANEL_ALT, ACCENT,
                                           1.0 - k / float(RADAR_TRAIL + 1)))

        # 当前扫描线
        th = math.radians(self.radar_disp)
        c.create_line(cx, cy, cx - r * math.cos(th), cy - r * math.sin(th),
                      fill=ACCENT, width=max(1, _sp(2)))
        center_r = _sp(3)
        c.create_oval(cx - center_r, cy - center_r, cx + center_r, cy + center_r,
                      fill=ACCENT, outline="")

        # 读数
        c.create_text(_sp(8), _sp(10), text=f"角度 {self.radar_disp:5.1f}°",
                      fill=FG, anchor="w", font=(FONT_UI, 10))
        c.create_text(w - _sp(8), _sp(10), text=f"量程 {RADAR_MAX_CM}cm",
                      fill=MUTED, anchor="e", font=("Consolas", 9))
        dist_txt = f"{self.radar_last_dist} cm" if self.radar_last_dist >= 0 else "---"
        c.create_text(_sp(8), h - _sp(10), text=f"距离 {dist_txt}",
                      fill=MUTED, anchor="w", font=(FONT_UI, 10))

        if not self.radar_pts:
            c.create_text(cx, cy - r * 0.55, text="等待回波…", fill=MUTED)

    def _push_history(self, value):
        self.history.append(value)
        if len(self.history) > HISTORY:
            self.history.pop(0)
        self._draw_chart()

    def _draw_chart(self):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), _sp(200))
        h = max(c.winfo_height(), _sp(100))
        if len(self.history) < 2:
            c.create_text(w // 2, h // 2, text="等待数据…", fill=MUTED)
            return
        lo = min(self.history)
        hi = max(self.history)
        if hi - lo < 1.0:
            hi = lo + 1.0
        step = max(1, w - _sp(16)) / (HISTORY - 1)
        pts = []
        for i, v in enumerate(self.history):
            x = _sp(8) + i * step
            y = h - _sp(14) - (v - lo) / (hi - lo) * (h - _sp(34))
            pts.extend([x, y])
        c.create_line(*pts, fill=ACCENT, width=max(1, _sp(2)), smooth=True)
        c.create_text(_sp(8), _sp(14), text=f"{hi:.1f}", fill=MUTED, anchor="w")
        c.create_text(_sp(8), h - _sp(14), text=f"{lo:.1f}", fill=MUTED, anchor="w")

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
    enable_dpi_awareness()
    root = tk.Tk()
    app = MonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
