# STM32 蓝牙上位机

配套 STM32F103 桌面终端项目的 PC 端工具：通过 HC-05 / HC-06 蓝牙串口（或 USB-TTL）查看传感器数据、控制舵机、调试通信协议。

- GitHub 仓库：<https://github.com/b-as-h/Host_computer-py>
- 固件协议：`Core/Src/bt.c`、`Core/Inc/bt.h`（固件工程不在本仓库）
- 界面技术：Python + Tkinter，无前端构建步骤
- 显示适配：Windows Per-Monitor V2 DPI，1080p / 2K 自适应
- 当前版本：**v1.9.0**
- 安装包：[STM32Host-Setup-1.9.0.exe](https://github.com/b-as-h/Host_computer-py/releases/download/v1.9.0/STM32Host-Setup-1.9.0.exe)
- 发布说明：<https://github.com/b-as-h/Host_computer-py/releases/tag/v1.9.0>

---

## 界面预览

以下截图全部由 [`tools/capture_screenshots.py`](tools/capture_screenshots.py) 使用固定虚拟数据生成，不需要开发板、串口或本机真实性能数据。

### 主界面

![主界面](screenshot.png)

### 温度曲线与 3D 姿态仪

![温度曲线与 3D 姿态仪](screenshots/trends.png)

### 3D 姿态仪特写

![3D 姿态仪](screenshots/gyro.png)

### 雷达扫描

![雷达扫描](screenshots/radar.png)

---

## 一、安装与启动

### 方式一：安装包（推荐）

1. 打开 [Releases](https://github.com/b-as-h/Host_computer-py/releases/latest)，下载
   `STM32Host-Setup-1.9.0.exe`。
2. 双击安装包。当前安装包未购买代码签名证书，Windows SmartScreen 可能提示
   “Windows 已保护你的电脑”，点 **更多信息 → 仍要运行** 即可。
3. 选择安装位置。默认安装到 `%LOCALAPPDATA%\Programs\STM32Host`，
   按当前用户安装，**不需要管理员权限**；可保留“创建桌面快捷方式”。
4. 安装完成后勾选“运行 STM32 Host 上位机”，或从桌面 / 开始菜单启动。
5. 卸载：Windows **设置 → 应用 → 已安装的应用 → STM32 Host 上位机 → 卸载**，
   或运行安装目录下的 `unins000.exe`。

安装包已包含程序、图标和本说明，内置 Python 运行时，目标电脑无需安装 Python 或 pyserial。

### 方式二：源码运行

**双击 `run_host.bat`**，或双击桌面快捷方式「STM32 Host 上位机」。

启动器会自动完成三件事：

1. 找一个带 tkinter 的 Python（依次尝试 `py -3` → `python` → miniconda → anaconda）；
2. 检查 pyserial，缺了就用 pip 安装（官方源失败自动换清华镜像）；
3. 以最小化控制台启动界面。

手动运行：

```bat
python -m pip install -r requirements.txt
python host_monitor.py
```

### 上板前先跑离线自检

```bat
python selftest.py
```

不需要开发板和串口。它用固件 `bt.c` 里真实的应答格式构造数据帧，喂给上位机的解析逻辑，核对三件事：

- 上位机发出的命令拼装是否正确（`PING` / `GET` / `SERVO,90` / `STREAM,1,500` / `BAUD,115200` …）；
- 收到的 `+DATA`、`$D` 帧能否解析出正确数值（含陀螺仪与姿态）；
- 异常帧、乱码、旧固件 6 字段帧会不会导致崩溃。

全部通过（退出码 0）再插板子。这样一旦上板出问题，就能确定是接线 / 波特率 / 供电，
而不是软件解析。改完协议先跑它，是成本最低的回归测试。

### 全功能前台自动化测试

```bat
python tools/system_test.py
python tools/system_test.py --hold
```

测试会打开主界面和独立测试台，用虚拟串口、性能快照和遥测帧覆盖通信、控制、
传感器、电脑状态、日志、异常恢复等模块。陀螺仪、温度曲线和雷达分别持续展示
6.4s、5.6s、5.6s，期间验证数据更新、动画绘制和状态切换。

运行结束自动生成：

- [`docs/system_test_report.md`](docs/system_test_report.md)：按模块列出覆盖、观测值和结论
- `test-results/system_test_results.json`：原始结果
- `test-results/system_gyro.png`、`system_temperature.png`、`system_radar.png`：阶段证据
- `test-results/system_test_hud.png`：测试台最终结果

### 重新生成展示截图

```bat
python tools/capture_screenshots.py
```

脚本固定 Tk 缩放为 1.25，使用虚拟串口、性能快照和遥测数据，输出：

- `screenshot.png`：主界面
- `screenshots/trends.png`：温度曲线与 3D 姿态仪
- `screenshots/gyro.png`：3D 姿态仪特写
- `screenshots/radar.png`：雷达扫描

运行环境需要至少 1400×1020 的 Windows 桌面，不连接任何真实硬件。

### 构建 exe 与安装包

已发布仓库：<https://github.com/b-as-h/Host_computer-py>

构建环境：Windows 10/11 64 位、Python 3.9+ 64 位、Inno Setup 7。

```bat
python -m pip install -r requirements-dev.txt
installer\build_installer.bat
```

脚本依次完成：

1. 安装 pyserial 与 PyInstaller；
2. PyInstaller 生成 `dist\STM32Host.exe`（单文件，含图标与 1.9.0 版本信息）；
3. Inno Setup 编译 `installer\STM32Host.iss`，生成
   `dist\STM32Host-Setup-1.9.0.exe`。

只重新生成 exe：

```bat
pyinstaller --noconfirm --clean --onefile --windowed --icon app.ico ^
  --version-file installer\version_info.txt ^
  --collect-all serial --name STM32Host host_monitor.py
```

**工具选择**：PyInstaller 负责把 Python 程序和运行时打成单文件 exe；Inno Setup 7 负责安装层。
相比 ZIP 或自解压包，Inno Setup 能生成标准 Windows 安装向导、开始菜单/桌面快捷方式、
“应用和功能”卸载项和版本信息，并且默认按当前用户安装，不触发 UAC。

> Inno Setup 7 下载：<https://jrsoftware.org/isdl.php>。安装脚本使用
> `PrivilegesRequired=lowest`，默认目录为 `%LOCALAPPDATA%\Programs\STM32Host`；
> `installer\build_installer.bat` 会自动查找 `ISCC.exe`。
>
> 安装包未做代码签名，SmartScreen 会提示未知发布者；正式分发前建议购买代码签名证书。
>
> conda 环境打包时，`_tkinter.pyd` 依赖的 `tcl90.dll` / `tcl9tk90.dll` 不会被自动收集，
> 还需按原环境手动追加 `--add-binary "%CONDA_PREFIX%\Library\bin\..."`。
> pyserial 使用 `--collect-all serial`，否则 exe 里可能检测不到串口。
> 若 conda 的 `_ctypes.pyd` 依赖独立 `ffi.dll`，也要一并加入，否则 `sysinfo` 会报采集模块不可用。

## 二、文件说明

| 文件 | 作用 |
|---|---|
| `host_monitor.py` | 主程序，Tkinter 图形界面，高 DPI 自适应，无第三方依赖（除 pyserial） |
| `sysinfo.py` | 本机性能采集（CPU/GPU/内存/网络/FPS/温度），纯 ctypes，可单独 `python sysinfo.py` 自测 |
| `run_host.bat` | 启动器，负责选解释器、装依赖、拉起界面 |
| `selftest.py` | 离线自检，不需要开发板和串口，验证协议解析是否正常 |
| `tools/system_test.py` | 前台系统级自动化测试，覆盖长时可视化、边界与异常场景 |
| `tools/capture_screenshots.py` | 用固定虚拟数据生成 README 展示截图 |
| `tools/gyro_sim.py` | 用虚拟 `+DATA` 序列驱动 3D 姿态仪，无需开发板和串口 |
| `docs/system_test_report.md` | 自动生成的模块化测试报告 |
| `test-results/` | 最近一次测试的 JSON、测试台和阶段截图 |
| `requirements.txt` | 运行依赖（pyserial） |
| `requirements-dev.txt` | 开发与打包依赖（PyInstaller） |
| `installer/STM32Host.iss` | Inno Setup 7 安装脚本（中文向导、快捷方式、卸载项） |
| `installer/version_info.txt` | exe 文件版本信息（1.9.0） |
| `installer/build_installer.bat` | 一键构建 exe 与安装包 |
| `dist/STM32Host.exe` | 单文件绿色版，免安装直接运行（约 11 MB） |
| `dist/STM32Host-Setup-1.9.0.exe` | 一键安装包（约 13 MB） |
| `screenshot.png` | 主界面截图，仓库首页用 |
| `screenshots/` | 趋势与雷达功能展示图 |
| `app.ico` | 图标（32/48/64 三档） |
| `README.md` | 本文档 |

## 三、环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 64 位（安装包与 exe 均为 x64） |
| Python | 仅源码运行需要；Python 3.9+ 64 位，且安装时勾选 `tcl/tk and IDLE` |
| 依赖 | pyserial（安装包已内置；源码模式由启动器自动安装） |
| 显示 | 1080p 及以上；支持 100% / 125% / 150% 系统缩放 |
| 硬件 | 蓝牙模块已与电脑配对，或 USB-TTL 直连开发板 |

## 四、连接步骤

1. 给开发板上电，HC-05 指示灯慢闪表示未配对。
2. Windows → 设置 → 蓝牙和其他设备 → 添加设备 → 选 `HC-05`，配对码通常是 `1234` 或 `0000`。
3. 配对后打开设备管理器 → 端口（COM 和 LPT），会出现**两个**口：一个「传入」、一个「传出」，选**传出**那个（例如 `COM5`）。
4. 上位机选串口、选波特率，点「连接」。
5. 程序会自动发一条 `PING`，日志里出现 `+PONG` 说明链路通了。

> 没有蓝牙模块时，用 USB-TTL 也行：模块 TX 接 PA10、RX 接 PA9，共地，插上 USB 后直接选新出现的 COM 口。

## 五、界面说明

| 区域 | 内容 |
|---|---|
| 顶部 | 产品标题、连接状态灯、串口下拉、刷新、**波特率下拉**、同步设备速率、连接 |
| 左栏顶部 | **电脑状态圆环**：CPU / GPU / RAM / NET / FPS，程序一打开就开始刷新，与串口是否连接无关 |
| 左栏 | 光照、温度、湿度、片内温度、供电电压、三轴加速度、**温度曲线 + 3D 姿态仪**（并排对齐）、**雷达视图**（在曲线下方，滚轮下滚查看） |
| 右栏 | 舵机角度滑条、舵机锁定、周期遥测开关、**雷达模式开关**、**发送电脑数据开关**、快捷命令按钮 |
| 发送栏 | 命令输入框（回车即发）、发送命令、清空日志 |
| 选项栏 | **追加 CRLF**、**HEX 发送**、时间戳（默认开启）、定时发送 + 间隔、命令历史提示 |
| 日志 | 收发记录，`>` 为发送、`<` 为接收、`!` 为异常 |

界面使用统一深色视觉规范，连接状态、按钮悬停、等待应答、操作成功/失败都有对应反馈。窗口会根据屏幕工作区和系统 DPI 自动选择 0.9–1.5 倍 UI 缩放，2K 与 1080p 下不会出现整体位图拉伸。

### 3D 姿态仪

位于温度曲线右侧，采用与主界面一致的深色内嵌仪表：**低饱和蓝灰 3D 立方体、柔和红 X / 绿 Y / 蓝 Z 轴箭头**。立方体随 `roll` / `pitch` 做三维旋转，底部同步显示姿态角和 `gx` / `gy` / `gz` 角速度。

- **姿态**：`roll` 归一化到 `-180°~+180°`，`pitch` 限制在 `-90°~+90°`；
- **颜色**：三轴使用降饱和的红 X、绿 Y、蓝 Z，立方体使用深蓝灰分层；与深色界面统一，避免高亮刺眼；
- **数值**：HUD 使用与动画相同的插值角度，避免数字提前跳变、立方体滞后；
- **自适应**：立方体、箭头和 HUD 均按 Canvas 尺寸与 DPI 缩放。

#### 数据接收与刷新机制

1. 串口读线程 `readline()` 收到一行；
2. 通过 `rx_queue` 交给 UI 主线程，由 50ms 一次的 `_poll()` 取出；
3. `+DATA,key=value,...` 或 11 字段 `$D` 经 `_handle_line()` 解析；
4. 完整且有限的 `roll/pitch` 更新动画目标，`gx/gy/gz` 更新 HUD；
5. 已有的 33ms `_animate()` 定时器以 30fps 对角度做时间相关缓动；
6. 到达目标后停止重绘，静止时不持续消耗 Canvas 性能。

`+DATA` 和 `$D` 共用同一套目标值更新逻辑。`GET` 返回的完整 `+DATA` 会立即触发，周期遥测 `$D` 按固件设置的周期触发。

#### 异常、暂停和断连

| 状态 | 判断 | 显示 |
|---|---|---|
| `LIVE` | 2 秒内收到有效姿态帧 | 绿色状态点，立方体正常动画 |
| `DATA ERR` | NaN/Inf、非法数值，或姿态字段不完整 | 红色状态，拒绝本帧并保留上一姿态 1.2 秒 |
| `HOLD` | 超过 2 秒没有新的姿态帧 | 橙色状态，保留最后姿态并显示“数据暂停” |
| `OFFLINE` | 串口断开/尚未连接 | 灰色半透明视觉，保留最后姿态并提示链路断开 |
| `WAIT` | 刚连接但尚未收到姿态 | 灰色立方体，等待第一帧 |

角度不会因单帧异常归零；断连也不会把界面重置到中位，便于现场判断最后姿态。

#### 模拟器与测试数据

```bat
python tools/gyro_sim.py
```

模拟器每 700ms 注入一帧完整 `+DATA`，经过真实解析和 30fps 动画链路，不使用串口。测试序列：

```text
+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,az=997,servo=96,lock=0,gx=0,gy=0,gz=0,roll=0.0,pitch=0.0
+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,az=997,servo=96,lock=0,gx=0,gy=0,gz=15,roll=5.0,pitch=0.0
+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,az=997,servo=96,lock=0,gx=0,gy=0,gz=45,roll=30.0,pitch=-8.0
+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,az=997,servo=96,lock=0,gx=35,gy=-22,gz=-62,roll=-32.0,pitch=18.0
+$D,72,2486,58,15,-8,997,0,0,0,-120,300
```

自动化回归：

```bat
python selftest.py
```

第 `[8]` 组覆盖 `+DATA` 触发、首帧直接对齐、33ms 可见响应、约 0.5 秒收敛、±180° 最短路径、NaN 拒收、角度钳位、HOLD 和 OFFLINE，共 13 项。

### 雷达视图

勾选右栏「雷达模式 60°/s」后，上位机会发 `RADAR,1,60`，固件开始 0°↔180° 来回扫描，
并以 **20Hz** 回传 `$R,<角度>,<距离cm>` 帧。左栏会自动滚到底部，露出雷达视图：

- **半圆坐标系**：0° 在左、90° 朝上、180° 在右，每 30° 一条刻度线，四道距离环；
- **扫描线动画**：固件只有 20Hz，界面按 30fps 本地补间，并在身后留一段渐隐拖尾；
- **回波点**：每个有有效距离的角度画一个点，近距离绿、远距离橙；
- **读数**：左上角实时角度，左下角最近一次距离，右上角量程；
- **量程** 固定 200cm，超出量程的点压在边缘。

> 左栏内容比窗口高，用**鼠标滚轮**上下滚动查看（指针要在左栏内，不会和日志框抢事件），
> 也可以拖右侧滚动条。温度曲线和 3D 姿态仪给了同样的高度，两者顶边严格对齐。

> 距离字段目前固定为 `-1`（超声波模块尚未接入），所以只会看到扫描线转动、
> `ECHO 0`。接上传感器后固件侧只需替换 `bt.c` 的 `radar_measure_cm()`，
> 上位机无需改动，回波点会自动出现。
>
> `$R` 帧不写入日志（20Hz 会把有用信息冲掉），其它帧照常记录。

### 常用功能细节

| 功能 | 说明 |
|---|---|
| 追加 CRLF | 默认开启，发送命令时自动补 `\r\n`；取消后原样发送（固件按 `\r` 或 `\n` 断行） |
| HEX 发送 | 支持 `AA 55 01` 和 `aa,55,0x01` 两种写法，开启后忽略回车选项 |
| 定时发送 | 按设定间隔重复发送输入框内容，最小 50ms |
| 命令历史 | 输入框内按 `↑` / `↓` 翻历史命令 |
| 快捷命令 | PING / GET / SYS / VER / HELP / RESET 六个按钮，等价于在输入框里输入 |
| PONG 超时 | 连接后 1.5 秒没收到 `+PONG`，状态栏变黄提示检查供电和波特率 |

## 六、蓝牙协议

USART1，8N1。命令以 `\n` 结尾，应答以 `\r\n` 结尾，全部为 ASCII。

| 命令 | 说明 | 示例应答 |
|---|---|---|
| `PING` | 心跳 | `+PONG` |
| `GET` | 读取一次全部数据 | `+DATA,light=85,temp=25.34,...` |
| `SERVO,<0-180>` | 设置舵机角度；锁定状态下拒绝 | `+OK` / `-ERR,LOCKED` |
| `SWEEP,<dps>` | 纯扫描，不上报；dps=0 停止 | `+OK` |
| `RADAR,<0/1>[,<dps>]` | 雷达模式：扫描 + 20Hz 上报 `$R` 帧 | `+OK,RADAR=1` |
| `PC,cpu=..,mem=..,...` | 上位机每秒推送的电脑状态帧（见下） | 固件支持后无应答 |
| `LOCK,<0/1>` | 舵机解锁 / 锁定；锁定会退出雷达模式 | `+OK` |
| `STREAM,<0/1>[,<ms>]` | 开关周期遥测，周期 100~5000ms | `+OK,STREAM=1` |
| `BAUD,<rate>` | 切换波特率，掉电恢复 9600 | `+OK,BAUD` |
| `SYS` | 复位原因、运行时间、内存、负载、波特率 | `+SYS,rst=POR,up=123,...` |
| `VER` | 固件版本 | `+VER,BASH-STM32,1.4.0` |
| `HELP` | 命令列表 | `+HELP,...` |
| `RESET` | 软复位 | `+BYE` |
| 其他 | 未知命令 | `-ERR,UNKNOWN` |

### 数据字段

`GET` 应答：

```
+DATA,light=85,temp=25.34,humi=60,chip=29.8,vdd=3280,ax=12,ay=-3,az=998,servo=90,lock=0,gx=37,gy=-18,gz=112,roll=-11.4,pitch=34.2
```

| 字段 | 含义 | 单位 |
|---|---|---|
| `light` | 光照强度 | % |
| `temp` | DS18B20 温度 | ℃（两位小数） |
| `humi` | DHT11 湿度 | % |
| `chip` | 片内温度 | ℃（一位小数） |
| `vdd` | 供电电压（VREFINT 反推） | mV |
| `ax` `ay` `az` | 三轴加速度 | mg |
| `servo` | 舵机角度 | ° |
| `lock` | 舵机锁定状态 | 0/1 |
| `gx` `gy` `gz` | 三轴角速度（陀螺仪） | °/s（取整） |
| `roll` `pitch` | 卡尔曼滤波姿态角 | °（一位小数） |

周期遥测帧（11 个字段，旧版上位机只取前 6 个）：

```
$D,<light>,<temp×100>,<humi>,<ax_mg>,<ay_mg>,<az_mg>,<gx>,<gy>,<gz>,<roll×10>,<pitch×10>
```

雷达帧（`RADAR,1` 期间每 50ms 一帧，20Hz）：

```
$R,<角度 0-180>,<距离 cm>
```

| 字段 | 含义 |
|---|---|
| 角度 | 云台当前角度，0~180° |
| 距离 | 0~400 cm；**-1 表示本次没有有效回波**（超声波未接入时恒为 -1） |

### 电脑状态帧（上位机 → STM32）

勾选右栏「发送电脑数据 1s」后，上位机每秒推一帧。全整数，MCU 端用 `atoi` 就能解，不用解析浮点：

```
PC,cpu=21,mem=63,gpu=6,ct=-1,gt=-1,cf=4007,gc=-1,ru=101,rt=160,dn=7,up=12,fps=75
```

| 字段 | 含义 | 单位 |
|---|---|---|
| `cpu` `mem` `gpu` | 占用率 | % |
| `ct` `gt` | CPU / GPU 温度，**-1 表示读不到** | ℃ |
| `cf` `gc` | CPU / GPU 频率，**-1 表示读不到** | MHz |
| `ru` `rt` | 已用 / 总内存 | 0.1 GB（`101` = 10.1 GB） |
| `dn` `up` | 网络下行 / 上行 | KB/s |
| `fps` | 桌面合成帧率 | 帧/秒 |

整帧约 80 字节，9600 波特率下传输约 83 ms，1 s 一帧绰绰有余。

> **当前固件还不认识 `PC` 命令**，会回 `-ERR,UNKNOWN`。上位机在推送期间会自动吞掉这些回复（只提示一次），不影响其它功能。
> 要让 STM32 真正显示，需要在 `bt.c` 的 `bt_exec()` 里加一个 `PC` 分支来解析这些字段。

`SYS` 应答：

```
+SYS,rst=POR,up=123,flash=34964,ram=5856,stack=312,loop=1520,lmax=12,baud=9600
```

| 字段 | 含义 |
|---|---|
| `rst` | 复位原因：POR / PIN / SW / IWDG / WWDG / LPWR |
| `up` | 上电至今秒数 |
| `flash` `ram` | 已用字节数 |
| `stack` | 栈峰值字节数 |
| `loop` `lmax` | 主循环频率（次/秒）、最近 1 秒内最长单次耗时（ms） |
| `baud` | 当前串口波特率 |

## 七、修改波特率

固件上电默认 **9600**。两侧速率必须一致，否则收不到任何数据。

### 情况 A：设备已经是目标速率（例如模块出厂就配成 115200）

直接在顶部下拉框选 115200 → 点「连接」即可。

### 情况 B：设备还是 9600，想改成 115200

不要直接改下拉框重连——设备还在旧速率上，必然失联。正确顺序：

```
连接中把下拉框切到 115200
        ↓
点「同步设备速率」
        ↓
上位机在旧链路(9600)上发 BAUD,115200     ← 命令必须走旧速率
        ↓
设备回 +OK,BAUD，发送缓冲排空后切换
        ↓
上位机收到确认，自动用 115200 重连
```

安全设计：

- 只有收到 `+OK,BAUD` 才会重连；
- 2 秒内没确认，下拉框自动退回原速率，不会把链路弄断；
- 波特率不写 Flash，掉电后固件恢复 9600。

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| 下拉框里没有串口 | 模块没配对成功，或 CH340 驱动没装 |
| 连上但只有 `> PING` 没有回 | 波特率不一致；或 PA9/PA10 接反；或模块没上电 |
| 状态栏显示「已连接，无应答」 | 同上，1.5 秒内没收到 `+PONG` |
| `SERVO` 返回 `-ERR,LOCKED` | 舵机处于锁定状态，先发 `LOCK,0` |
| `-ERR,BAUD` | 波特率不在 9600/19200/38400/57600/115200 之内 |
| 双击 bat 一闪而过 | 用 cmd 手动运行 `python host_monitor.py` 看报错 |
| 图标显示成白纸 | 桌面按 F5 刷新，或重建图标缓存 |
| 改了 `host_monitor.py` 但 exe 没变化 | 源码不会自动进入旧 exe，需重新执行 PyInstaller |
| 界面文字发虚 | 确认使用新版 exe；旧版未声明 DPI 感知，在 125%/150% 缩放下会被系统拉伸 |

## 九、自己扩展

- 新增命令：固件 `Core/Src/bt.c` 的 `bt_exec()` 加分支，上位机在右栏「快捷命令」数组里加按钮。
- 改界面配色：`host_monitor.py` 顶部的 `BG` / `PANEL` / `ACCENT` 常量。
- 改默认波特率：固件 `Core/Src/usart.c` 的 `huart1.Init.BaudRate`，以及上位机 `DEFAULT_BAUD`。

## 十、更新记录

| 版本 | 内容 |
|---|---|
| v1.9.0 | 新增 **Inno Setup 7 一键安装包**（中文向导、开始菜单/桌面快捷方式、标准卸载项、按用户安装）；exe 写入 1.9.0 文件版本信息；补充 `requirements.txt` / `requirements-dev.txt` 与可复现构建脚本；清理 `__pycache__`、旧构建临时目录和误建空文件；README 增加安装、卸载、构建与 SmartScreen 说明 |
| v1.8 | 新增前台系统测试台：55 项 E2E 覆盖通信、控制、传感器、界面、异常恢复；陀螺仪、温度曲线、雷达长时验证均超过 5 秒；自动生成模块报告、JSON 和阶段截图；修复温度曲线平滑线及标签 1px 越界 |
| v1.7 | 重做 **3D 姿态仪**：深色低饱和仪表、蓝灰立方体、柔和红 X / 绿 Y / 蓝 Z 轴；姿态由 `+DATA` / `$D` 驱动，30fps 时间缓动；新增 `LIVE / WAIT / HOLD / DATA ERR / OFFLINE` 状态、NaN 拒收、角度钳位和断连保持；新增 `tools/gyro_sim.py` 模拟器与 13 项姿态回归测试，自检 79 项全部通过 |
| v1.6 | 重构界面视觉与信息层级，统一深色配色、字体、间距、圆角卡片和按钮状态；增加**Per-Monitor V2 DPI 感知**及 1080p / 2K 自适应缩放；重绘 Canvas 仪表、图表、陀螺仪、雷达和 DPI 复选框；新增虚拟数据截图工具与四张全功能展示图 |
| v1.5 | 增加**电脑状态圆环**（CPU / GPU / RAM / NET / FPS，打开即刷新）与「发送电脑数据 1s」开关；新增 `sysinfo.py`（纯 ctypes 采集，含 PDH GPU 计数器、DWM 帧率、HWiNFO 温度）；协议增加 `PC,...` 帧；`selftest.py` 增加 9 项 PC 帧校验 |
| v1.4 | 增加**雷达视图**（半圆极坐标 + 30fps 补间扫描 + 渐隐拖尾 + 回波点）与「雷达模式」开关；协议增加 `RADAR,<0/1>[,<dps>]` 与 `$R` 帧；时间戳默认开启；修复 `clam` 主题复选框显示成"叉"的问题；左栏改为滚轮滚动，雷达图移到温度曲线下方、曲线与陀螺仪顶边对齐、移除重复的舵机读数；`selftest.py` 覆盖雷达收发 |
| v1.3 | 增加 `selftest.py` 离线自检；`+DATA` 的 `lock` / `servo` 字段回写到复选框与滑条（仅 `+DATA`，周期遥测不回写，避免覆盖用户操作） |
| v1.2 | 增加陀螺仪面板（姿态气泡 + 三轴角速度条）；`GET` 与 `$D` 增加 `gx/gy/gz/roll/pitch` 字段；修复右侧控制栏被左栏挤压的问题 |
| v1.1 | 增加波特率切换与同步设备波特率；增加自动回车、HEX 发送、时间戳、定时发送、命令历史、清空日志、快捷命令按钮；增加 PONG 超时提示 |
| v1.0 | 首个版本：串口连接、实时数据、温度曲线、舵机控制、周期遥测、原始日志 |
