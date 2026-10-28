# -*- coding: utf-8 -*-
"""
Windows 系统信息采集（纯 ctypes，不引入任何第三方依赖）

给 host_monitor.py 用：后台线程每秒采一次，UI 侧随时取快照。

采集项与桌宠上位机（C++/Qt 版）保持一致：
    CPU 占用 / CPU 频率 / CPU 温度
    GPU 占用 / GPU 频率 / GPU 温度
    内存占用 + 已用/总量
    网络上下行
    桌面合成帧率 + 刷新率

几处必须注意的实现细节（都是实测踩出来的）：
  1. GetSystemTimes 第一次采样必须丢弃，且 kernel time 里已含 idle time
  2. GPU 占用走 PDH 的 GPU Engine 计数器，不能用 NVML（只认 N 卡）
  3. GetIfTable2 会把 NDIS 过滤驱动接口也当网卡返回，必须按 HardwareInterface 过滤
  4. PDH 计数器句柄必须保存，传 NULL 之后再取值会崩
  5. DWM 帧计数不是每个 vblank 都 +1，桌面静止时要用刷新率兜底
  6. 温度只能从 HWiNFO 共享内存读，没装就是 -1
"""

import ctypes
import threading
import time
from ctypes import wintypes

# 这些 API 分散在四个 DLL 里，不能都挂 kernel32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
pdh_dll = ctypes.WinDLL("pdh")
iphlpapi = ctypes.WinDLL("iphlpapi")
dwmapi = ctypes.WinDLL("dwmapi")

# 64 位下返回指针的函数必须声明 restype，否则默认按 int 截断成 32 位
kernel32.OpenFileMappingW.restype = ctypes.c_void_p
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.VirtualQuery.restype = ctypes.c_size_t
iphlpapi.GetIfTable2.restype = wintypes.DWORD
dwmapi.DwmGetCompositionTimingInfo.restype = ctypes.c_long

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0

FILE_MAP_READ = 0x0004


# ============================================================================
# CPU 占用：GetSystemTimes
# ============================================================================
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


def _ft_to_u64(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


# ============================================================================
# 内存：GlobalMemoryStatusEx
# ============================================================================
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# ============================================================================
# 网络：GetIfTable2（MIB_IF_ROW2 字段很多，按 C 定义顺序写全，让 ctypes 自己算偏移）
# ============================================================================
class _MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_ulonglong),
        ("InterfaceIndex", wintypes.DWORD),
        ("InterfaceGuid", ctypes.c_ubyte * 16),
        ("Alias", wintypes.WCHAR * 257),
        ("Description", wintypes.WCHAR * 257),
        ("PhysicalAddressLength", wintypes.DWORD),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TunnelType", wintypes.DWORD),
        ("MediaType", wintypes.DWORD),
        ("PhysicalMediumType", wintypes.DWORD),
        ("AccessType", wintypes.DWORD),
        ("DirectionType", wintypes.DWORD),
        # 联合体：低 2 位分别是 HardwareInterface / FilterInterface
        ("InterfaceAndOperStatusFlags", wintypes.DWORD),
        ("OperStatus", wintypes.DWORD),
        ("AdminStatus", wintypes.DWORD),
        ("MediaConnectState", wintypes.DWORD),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("ConnectionType", wintypes.DWORD),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("InOctets", ctypes.c_ulonglong),
        ("InUcastPkts", ctypes.c_ulonglong),
        ("InNUcastPkts", ctypes.c_ulonglong),
        ("InDiscards", ctypes.c_ulonglong),
        ("InErrors", ctypes.c_ulonglong),
        ("InUnknownProtos", ctypes.c_ulonglong),
        ("InUcastOctets", ctypes.c_ulonglong),
        ("InMulticastOctets", ctypes.c_ulonglong),
        ("InBroadcastOctets", ctypes.c_ulonglong),
        ("OutOctets", ctypes.c_ulonglong),
        ("OutUcastPkts", ctypes.c_ulonglong),
        ("OutNUcastPkts", ctypes.c_ulonglong),
        ("OutDiscards", ctypes.c_ulonglong),
        ("OutErrors", ctypes.c_ulonglong),
        ("OutUcastOctets", ctypes.c_ulonglong),
        ("OutMulticastOctets", ctypes.c_ulonglong),
        ("OutBroadcastOctets", ctypes.c_ulonglong),
        ("OutQLen", ctypes.c_ulonglong),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG),
                ("Table", _MIB_IF_ROW2 * 1)]


# ============================================================================
# DWM：帧率
# ============================================================================
class _UNSIGNED_RATIO(ctypes.Structure):
    _fields_ = [("uiNumerator", wintypes.UINT),
                ("uiDenominator", wintypes.UINT)]


class _DWM_TIMING_INFO(ctypes.Structure):
    # 必须按 4 字节对齐！用 C 实测 sizeof = 292，而 ctypes 默认 8 字节对齐会算成 320，
    # 传进去 DWM 会以参数非法拒绝（HRESULT 0x88980090）。
    _pack_ = 4
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("rateRefresh", _UNSIGNED_RATIO),
        ("qpcRefreshPeriod", ctypes.c_ulonglong),
        ("rateCompose", _UNSIGNED_RATIO),
        ("qpcVBlank", ctypes.c_ulonglong),
        ("cRefresh", ctypes.c_ulonglong),
        ("cDXRefresh", wintypes.UINT),
        ("qpcCompose", ctypes.c_ulonglong),
        ("cFrame", ctypes.c_ulonglong),
        ("cDXPresent", wintypes.UINT),
        ("cRefreshFrame", ctypes.c_ulonglong),
        ("cFrameSubmitted", ctypes.c_ulonglong),
        ("cDXPresentSubmitted", wintypes.UINT),
        ("cFrameConfirmed", ctypes.c_ulonglong),
        ("cDXPresentConfirmed", wintypes.UINT),
        ("cRefreshConfirmed", ctypes.c_ulonglong),
        ("cDXRefreshConfirmed", wintypes.UINT),
        ("cFramesLate", ctypes.c_ulonglong),
        ("cFramesOutstanding", wintypes.UINT),
        ("cFrameDisplayed", ctypes.c_ulonglong),
        ("qpcFrameDisplayed", ctypes.c_ulonglong),
        ("cRefreshFrameDisplayed", ctypes.c_ulonglong),
        ("cFrameComplete", ctypes.c_ulonglong),
        ("qpcFrameComplete", ctypes.c_ulonglong),
        ("cFramePending", ctypes.c_ulonglong),
        ("qpcFramePending", ctypes.c_ulonglong),
        ("cFramesDisplayed", ctypes.c_ulonglong),
        ("cFramesComplete", ctypes.c_ulonglong),
        ("cFramesPending", ctypes.c_ulonglong),
        ("cFramesAvailable", ctypes.c_ulonglong),
        ("cFramesDropped", ctypes.c_ulonglong),
        ("cFramesMissed", ctypes.c_ulonglong),
        ("cRefreshNextDisplayed", ctypes.c_ulonglong),
        ("cRefreshNextPresented", ctypes.c_ulonglong),
        ("cRefreshesDisplayed", ctypes.c_ulonglong),
        ("cRefreshesPresented", ctypes.c_ulonglong),
        ("cRefreshStarted", ctypes.c_ulonglong),
        ("cPixelsReceived", ctypes.c_ulonglong),
        ("cPixelsDrawn", ctypes.c_ulonglong),
        ("cBuffersEmpty", ctypes.c_ulonglong),
    ]


# ============================================================================
# HWiNFO 共享内存（温度 / GPU 频率）
# ============================================================================
_HW_OFF_SIG = 0
_HW_OFF_POLL = 16
_HW_OFF_READ_SEC = 36
_HW_OFF_READ_SZ = 40
_HW_OFF_READ_CNT = 44
_HW_HDR_MIN = 48

_HW_R_TYPE = 8
_HW_R_LABEL = 140
_HW_R_VALUE = 288
_HW_R_MIN = 296

_HW_TYPE_TEMP = 1
_HW_TYPE_CLOCK = 5

_HW_MAP_NAMES = ("Global\\HWiNFO_SENS_SM2", "Global\\HWiNFO_SENS_SM",
                 "HWiNFO_SENS_SM2", "HWiNFO_SENS_SM")


def _score_cpu_temp(label):
    if "CPU" not in label:
        return -1
    s = 10
    if "Package" in label:
        s += 50
    if "Tctl" in label or "Tdie" in label:
        s += 60
    if "Core" in label:
        s += 15
    if "Max" in label:
        s -= 40
    if "Distance" in label or "to TjMax" in label:
        s -= 60
    return s


def _score_gpu_temp(label):
    if "GPU" not in label:
        return -1
    s = 10
    if "Temperature" in label:
        s += 50
    if "Hot Spot" in label:
        s -= 35
    if "Memory" in label:
        s -= 20
    if "Max" in label:
        s -= 40
    return s


def _score_gpu_clock(label):
    if "GPU" not in label:
        return -1
    s = 10
    if "Clock" in label:
        s += 50
    if "Memory" in label:
        s -= 20
    if "Max" in label:
        s -= 40
    return s


class _PdhQuery:
    """PDH 查询的薄封装。句柄一定要留着，传 None 之后取值会崩。"""

    def __init__(self):
        self.query = ctypes.c_void_p()
        self.counters = []          # [(handle, tag)]
        self.ok = False

    def open(self):
        q = ctypes.c_void_p()
        if pdh_dll.PdhOpenQueryW(None, 0, ctypes.byref(q)) != ERROR_SUCCESS:
            return False
        self.query = q
        self.ok = True
        return True

    def add(self, path, tag=None):
        h = ctypes.c_void_p()
        if pdh_dll.PdhAddCounterW(self.query, path, 0, ctypes.byref(h)) != ERROR_SUCCESS:
            return False
        self.counters.append((h, tag))
        return True

    def add_wildcard(self, pattern):
        """展开通配路径后逐个添加，tag 从实例名里的 engtype_ 提取。"""
        size = wintypes.DWORD(0)
        st = pdh_dll.PdhExpandWildCardPathW(None, pattern, None,
                                            ctypes.byref(size), 0)
        # PDH 状态码是有符号 LONG，0x800007D2 会变成负数，必须按无符号比
        if (st & 0xFFFFFFFF) not in (PDH_MORE_DATA, ERROR_SUCCESS) or size.value == 0:
            return 0
        buf = ctypes.create_unicode_buffer(size.value + 4)
        st = pdh_dll.PdhExpandWildCardPathW(None, pattern, buf,
                                            ctypes.byref(size), 0)
        if (st & 0xFFFFFFFF) != ERROR_SUCCESS:
            return 0

        # 返回的是"多字符串"：若干条路径用 \0 分隔、末尾双 \0。
        # 注意不能用 buf[0] 取，那只会拿到第一个字符；要整体读出来再切。
        raw = ctypes.wstring_at(ctypes.addressof(buf), size.value)

        added = 0
        for path in raw.split("\0"):
            if not path:
                continue
            tag = ""
            idx = path.find("engtype_")
            if idx >= 0:
                end = path.find(")", idx)
                tag = path[idx + 8: end if end > 0 else None]
            if tag and self.add(path, tag):
                added += 1
        return added

    def collect(self):
        if not self.ok:
            return False
        return pdh_dll.PdhCollectQueryData(self.query) == ERROR_SUCCESS

    def value(self, handle):
        val = _PDH_FMT_COUNTERVALUE()
        if pdh_dll.PdhGetFormattedCounterValue(handle, PDH_FMT_DOUBLE,
                                                None, ctypes.byref(val)) != ERROR_SUCCESS:
            return None
        if val.CStatus != ERROR_SUCCESS:
            return None
        return val.doubleValue

    def close(self):
        if self.ok and self.query:
            pdh_dll.PdhCloseQuery(self.query)
        self.ok = False
        self.counters = []


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD),
                ("doubleValue", ctypes.c_double)]


# ============================================================================
# 采集器
# ============================================================================
class Collector(threading.Thread):
    """后台线程，每秒采一次，UI 用 snapshot() 取最新值。"""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True, name="sysinfo")
        self.interval = interval
        self._lock = threading.Lock()
        self._data = {
            "cpu": 0.0, "mem": 0.0, "gpu": -1.0,
            "cpu_temp": -1.0, "gpu_temp": -1.0,
            "cpu_mhz": -1.0, "gpu_mhz": -1.0,
            "ram_used": 0.0, "ram_total": 0.0,
            "net_down": 0.0, "net_up": 0.0,
            "fps": -1.0, "hz": -1.0,
        }
        self._stop = threading.Event()

        # CPU
        self._prev_idle = 0
        self._prev_kernel = 0
        self._prev_user = 0
        self._has_prev = False

        # 网络
        self._net_prev = None
        self._net_ms = 0

        # DWM
        self._dwm_prev_frames = None
        self._dwm_prev_ms = 0
        self._dwm_idle = 0
        self._dwm_last_good = -1.0

        # HWiNFO
        self._hw_addr = None
        self._hw_size = 0
        self._hw_offsets = (-1, -1, -1)
        self._hw_poll_prev = -1
        self._hw_stale = 0
        self._hw_try_ms = 0

        # PDH
        self._gpu_q = None
        self._freq_q = None
        self._base_mhz = 0.0

        self._init_pdh()
        self._init_cpu_baseline()

    # ---------- 初始化 ----------
    def _init_cpu_baseline(self):
        i, k, u = _FILETIME(), _FILETIME(), _FILETIME()
        if kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
            self._prev_idle = _ft_to_u64(i)
            self._prev_kernel = _ft_to_u64(k)
            self._prev_user = _ft_to_u64(u)
            self._has_prev = True

    def _init_pdh(self):
        # GPU
        q = _PdhQuery()
        if q.open() and q.add_wildcard("\\GPU Engine(*)\\Utilization Percentage") > 0:
            q.collect()
            self._gpu_q = q
        else:
            q.close()

        # CPU 频率：标称频率 × % Processor Performance
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            self._base_mhz = float(winreg.QueryValueEx(key, "~MHz")[0])
            winreg.CloseKey(key)
        except Exception:
            self._base_mhz = 0.0

        if self._base_mhz > 0:
            fq = _PdhQuery()
            if fq.open() and fq.add(
                    "\\Processor Information(_Total)\\% Processor Performance"):
                fq.collect()
                self._freq_q = fq
            else:
                fq.close()

    # ---------- 单项采集 ----------
    def _read_cpu(self):
        i, k, u = _FILETIME(), _FILETIME(), _FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
            return None
        ci, ck, cu = _ft_to_u64(i), _ft_to_u64(k), _ft_to_u64(u)
        pct = None
        if self._has_prev:
            d_idle = ci - self._prev_idle
            d_kernel = ck - self._prev_kernel
            d_user = cu - self._prev_user
            total = d_kernel + d_user
            if total > 0:
                busy = total - d_idle      # kernel 里已含 idle
                pct = max(0.0, min(100.0, busy * 100.0 / total))
        self._prev_idle, self._prev_kernel, self._prev_user = ci, ck, cu
        self._has_prev = True
        return pct

    def _read_mem(self):
        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        return (float(ms.dwMemoryLoad),
                (ms.ullTotalPhys - ms.ullAvailPhys) / 1073741824.0,
                ms.ullTotalPhys / 1073741824.0)

    def _read_gpu(self):
        q = self._gpu_q
        if q is None or not q.collect():
            return None
        by_engine = {}
        for h, tag in q.counters:
            v = q.value(h)
            if v is None:
                continue
            by_engine[tag] = by_engine.get(tag, 0.0) + v
        if not by_engine:
            return None
        if "3D" in by_engine:
            return min(100.0, by_engine["3D"])
        return min(100.0, max(by_engine.values()))

    def _read_freq(self):
        q = self._freq_q
        if q is None or not q.collect():
            return None
        v = q.value(q.counters[0][0])
        if v is None:
            return None
        return self._base_mhz * v / 100.0

    def _read_net(self):
        tbl = ctypes.POINTER(_MIB_IF_TABLE2)()
        if iphlpapi.GetIfTable2(ctypes.byref(tbl)) != 0 or not tbl:
            return None
        try:
            n = tbl.contents.NumEntries
            base = ctypes.addressof(tbl.contents) + _MIB_IF_TABLE2.Table.offset
            row_sz = ctypes.sizeof(_MIB_IF_ROW2)
            in_oct = 0
            out_oct = 0
            for i in range(n):
                r = _MIB_IF_ROW2.from_address(base + i * row_sz)
                # 只要以太网/无线，且必须是真实硬件接口
                if r.Type not in (6, 71):
                    continue
                if r.OperStatus != 1:            # IfOperStatusUp
                    continue
                if r.MediaConnectState != 1:     # MediaConnectStateConnected
                    continue
                flags = r.InterfaceAndOperStatusFlags
                if not (flags & 0x1):            # HardwareInterface
                    continue
                if flags & 0x2:                  # FilterInterface
                    continue
                desc = r.Description
                if any(k in desc for k in ("Virtual", "Hyper-V", "VMware",
                                           "VirtualBox", "TAP", "WireGuard",
                                           "Loopback", "VPN", "WAN Miniport",
                                           "Kernel Debug")):
                    continue
                in_oct += r.InOctets
                out_oct += r.OutOctets
        finally:
            iphlpapi.FreeMibTable(tbl)

        now = int(time.time() * 1000)
        down = up = 0.0
        if self._net_prev is not None and now > self._net_ms:
            dt = (now - self._net_ms) / 1000.0
            d_in = max(0, in_oct - self._net_prev[0])
            d_out = max(0, out_oct - self._net_prev[1])
            if dt > 0:
                down = d_in / 1024.0 / dt
                up = d_out / 1024.0 / dt
        self._net_prev = (in_oct, out_oct)
        self._net_ms = now
        return down, up

    def _read_dwm(self):
        ti = _DWM_TIMING_INFO()
        ti.cbSize = ctypes.sizeof(_DWM_TIMING_INFO)
        if dwmapi.DwmGetCompositionTimingInfo(None, ctypes.byref(ti)) != 0:
            return None
        hz = -1.0
        if ti.rateRefresh.uiDenominator:
            hz = ti.rateRefresh.uiNumerator / float(ti.rateRefresh.uiDenominator)

        frames = ti.cFramesDisplayed
        now = int(time.time() * 1000)
        if self._dwm_prev_frames is None:
            self._dwm_prev_frames = frames
            self._dwm_prev_ms = now
            return hz, (hz if hz > 0 else -1.0)

        dt = (now - self._dwm_prev_ms) / 1000.0
        prev = self._dwm_prev_frames
        self._dwm_prev_frames = frames
        self._dwm_prev_ms = now
        if dt <= 0.05:
            return hz, (hz if hz > 0 else -1.0)

        f = max(0.0, (frames - prev) / dt) if frames >= prev else 0.0
        f = min(f, 400.0)

        if f >= 1.0:
            self._dwm_idle = 0
            self._dwm_last_good = f
        else:
            self._dwm_idle = min(self._dwm_idle + 1, 1000)

        # 桌面静止时 DWM 不产帧，用刷新率兜底，否则界面上永远是 0
        if self._dwm_idle >= 3:
            fps = hz if hz > 0 else -1.0
        elif f >= 1.0:
            fps = f
        else:
            fps = self._dwm_last_good if self._dwm_last_good > 0 else hz
        return hz, fps

    # ---------- HWiNFO ----------
    def _hw_attach(self):
        for name in _HW_MAP_NAMES:
            h = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
            if not h:
                continue
            addr = kernel32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
            kernel32.CloseHandle(h)
            if not addr:
                continue
            size = self._region_size(addr)
            if size < _HW_HDR_MIN:
                kernel32.UnmapViewOfFile(addr)
                continue
            if self._u32(addr, _HW_OFF_SIG) != 0x53695748:   # 'HWiS'
                kernel32.UnmapViewOfFile(addr)
                continue
            self._hw_addr = addr
            self._hw_size = size
            if self._hw_scan():
                return True
            kernel32.UnmapViewOfFile(addr)
            self._hw_addr = None
        return False

    @staticmethod
    def _region_size(addr):
        class _MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p),
                        ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wintypes.DWORD),
                        ("PartitionId", wintypes.WORD),
                        ("RegionSize", ctypes.c_size_t),
                        ("State", wintypes.DWORD),
                        ("Protect", wintypes.DWORD),
                        ("Type", wintypes.DWORD)]
        mbi = _MBI()
        if kernel32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi)) == 0:
            return 0
        return mbi.RegionSize

    def _u32(self, addr, off):
        return ctypes.c_uint32.from_address(addr + off).value

    def _i64(self, addr, off):
        return ctypes.c_int64.from_address(addr + off).value

    def _f64(self, addr, off):
        return ctypes.c_double.from_address(addr + off).value

    def _hw_scan(self):
        addr = self._hw_addr
        off_sec = self._u32(addr, _HW_OFF_READ_SEC)
        stride = self._u32(addr, _HW_OFF_READ_SZ)
        count = self._u32(addr, _HW_OFF_READ_CNT)
        if stride < _HW_R_MIN or count == 0:
            return False
        if off_sec + count * stride > self._hw_size:
            return False

        best = [-1, -1, -1]
        score = [-1, -1, -1]
        for i in range(count):
            base = addr + off_sec + i * stride
            rtype = self._u32(base, _HW_R_TYPE)
            if rtype not in (_HW_TYPE_TEMP, _HW_TYPE_CLOCK):
                continue
            raw = ctypes.string_at(base + _HW_R_LABEL, 128).split(b"\0", 1)[0]
            label = raw.decode("latin-1", "replace")
            if not label:
                raw = ctypes.string_at(base + 12, 128).split(b"\0", 1)[0]
                label = raw.decode("latin-1", "replace")
            if not label:
                continue
            off = off_sec + i * stride
            if rtype == _HW_TYPE_TEMP:
                s = _score_cpu_temp(label)
                if s > score[0]:
                    score[0], best[0] = s, off
                s = _score_gpu_temp(label)
                if s > score[1]:
                    score[1], best[1] = s, off
            else:
                s = _score_gpu_clock(label)
                if s > score[2]:
                    score[2], best[2] = s, off
        self._hw_offsets = tuple(best)
        return any(o >= 0 for o in best)

    def _hw_value(self, off):
        if off < 0 or not self._hw_addr:
            return -1.0
        if off + _HW_R_VALUE + 8 > self._hw_size:
            return -1.0
        v = self._f64(self._hw_addr, off + _HW_R_VALUE)
        if v < -900.0 or v > 100000.0:
            return -1.0
        return v

    def _read_hwinfo(self):
        now = int(time.time() * 1000)
        if self._hw_addr is None:
            if now - self._hw_try_ms < 5000:
                return None
            self._hw_try_ms = now
            if not self._hw_attach():
                return None

        pt = self._i64(self._hw_addr, _HW_OFF_POLL)
        if pt == self._hw_poll_prev:
            self._hw_stale += 1
            # 免费版 12 小时后会冻结，连续 15 秒不更新就当没数据
            if self._hw_stale > 15:
                return None
        else:
            self._hw_poll_prev = pt
            self._hw_stale = 0

        ct = self._hw_value(self._hw_offsets[0])
        gt = self._hw_value(self._hw_offsets[1])
        gc = self._hw_value(self._hw_offsets[2])
        return (ct if 0 <= ct < 150 else -1.0,
                gt if 0 <= gt < 150 else -1.0,
                gc if 0 < gc < 10000 else -1.0)

    # ---------- 主循环 ----------
    def run(self):
        while not self._stop.is_set():
            t0 = time.time()
            snap = {}
            try:
                cpu = self._read_cpu()
                if cpu is not None:
                    snap["cpu"] = cpu

                mem = self._read_mem()
                if mem is not None:
                    snap["mem"], snap["ram_used"], snap["ram_total"] = mem

                gpu = self._read_gpu()
                if gpu is not None:
                    snap["gpu"] = gpu

                freq = self._read_freq()
                if freq is not None:
                    snap["cpu_mhz"] = freq

                net = self._read_net()
                if net is not None:
                    snap["net_down"], snap["net_up"] = net

                dwm = self._read_dwm()
                if dwm is not None:
                    snap["hz"], snap["fps"] = dwm

                hw = self._read_hwinfo()
                if hw is not None:
                    snap["cpu_temp"], snap["gpu_temp"], snap["gpu_mhz"] = hw
            except Exception:
                # 采集失败不能把线程带崩，界面继续显示上一帧
                pass

            with self._lock:
                self._data.update(snap)

            dt = time.time() - t0
            self._stop.wait(max(0.05, self.interval - dt))

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return dict(self._data)


# ============================================================================
# 独立自测：python sysinfo.py
# ============================================================================
if __name__ == "__main__":
    c = Collector()
    c.start()
    try:
        for _ in range(6):
            time.sleep(1)
            d = c.snapshot()
            print("cpu=%5.1f%% mem=%5.1f%% gpu=%5.1f%% | cT=%5.1f gT=%5.1f "
                  "cF=%6.0f gC=%5.0f | ram=%4.1f/%4.1fG net=%7.1f/%7.1f KB/s "
                  "| fps=%5.1f hz=%3.0f"
                  % (d["cpu"], d["mem"], d["gpu"], d["cpu_temp"], d["gpu_temp"],
                     d["cpu_mhz"], d["gpu_mhz"], d["ram_used"], d["ram_total"],
                     d["net_down"], d["net_up"], d["fps"], d["hz"]))
    finally:
        c.stop()
