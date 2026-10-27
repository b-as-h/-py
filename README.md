# STM32 蓝牙上位机

配合 STM32F103 桌面终端项目的 PC 端串口上位机：通过 HC-05 / HC-06 蓝牙串口（或 USB-TTL）查看传感器数据、控制舵机、调试通信协议。

![界面截图](screenshot.png)

## 下载即用

**不需要装 Python**，直接下载 [`STM32Host.exe`](STM32Host.exe) 双击运行（约 13 MB，Win7+ 64 位）。

首次运行如果 Windows Defender 提示"未知发布者"，点「更多信息 → 仍要运行」即可。
这是 PyInstaller 打包程序的常见误报，源码完全公开在本仓库。

## 功能

| 模块 | 内容 |
|---|---|
| 连接 | 串口枚举、**波特率切换**（9600/19200/38400/57600/115200）、一键同步设备波特率 |
| 数据 | 光照、温度、湿度、片内温度、供电电压、舵机角度、三轴加速度、温度曲线 |
| 陀螺仪 | 姿态气泡（roll/pitch）+ 三轴角速度条（±250 °/s），颜色随幅度变化 |
| 控制 | 舵机角度滑条、舵机锁定、周期遥测开关 |
| 串口助手 | 自动追加回车、HEX 发送、时间戳、定时发送、命令历史（↑/↓）、清空日志 |
| 调试 | 快捷命令按钮、原始收发日志、PONG 超时告警 |

## 从源码运行

```bat
pip install pyserial
python host_monitor.py
```

或双击 `run_host.bat`——它会自动寻找带 tkinter 的 Python，缺 pyserial 时自动安装。

## 离线自检

不需要开发板和串口，用固件真实的应答格式喂给上位机解析逻辑：

```bat
python selftest.py
```

覆盖命令拼装、`+DATA` / `$D` 帧解析、旧固件兼容、异常帧健壮性、波特率切换流程，共 44 项。
改完协议先跑它，是成本最低的回归测试。

## 通信协议

USART1，8N1，命令以 `\n` 结尾，应答以 `\r\n` 结尾，全部 ASCII。

| 命令 | 说明 | 示例应答 |
|---|---|---|
| `PING` | 心跳 | `+PONG` |
| `GET` | 读取一次全部数据 | `+DATA,light=85,temp=25.34,...` |
| `SERVO,<0-180>` | 设置舵机角度（锁定状态下拒绝） | `+OK` / `-ERR,LOCKED` |
| `LOCK,<0/1>` | 舵机解锁 / 锁定 | `+OK` |
| `STREAM,<0/1>[,<ms>]` | 开关周期遥测，周期 100~5000ms | `+OK,STREAM=1` |
| `BAUD,<rate>` | 切换波特率，掉电恢复 9600 | `+OK,BAUD` |
| `SYS` | 复位原因、运行时间、内存、负载、波特率 | `+SYS,rst=POR,up=123,...` |
| `VER` | 固件版本 | `+VER,BASH-STM32,1.4.0` |
| `HELP` | 命令列表 | `+HELP,...` |
| `RESET` | 软复位 | `+BYE` |

### 周期遥测帧

```
$D,<light>,<temp×100>,<humi>,<ax_mg>,<ay_mg>,<az_mg>,<gx>,<gy>,<gz>,<roll×10>,<pitch×10>
```

例：`$D,72,2486,58,15,-8,997,37,-18,112,-114,342` → 光照 72%、24.86℃、湿度 58%、
加速度 (0.015, −0.008, 0.997) g、角速度 (37, −18, 112) °/s、roll −11.4°、pitch 34.2°。

### 查询应答

```
+DATA,light=72,temp=24.86,humi=58,chip=31.2,vdd=3284,ax=15,ay=-8,az=997,servo=90,lock=0,gx=37,gy=-18,gz=112,roll=-11.4,pitch=34.2
```

带字段名，不依赖位置；旧版上位机只取前 6 个 `$D` 字段，向后兼容。

## 修改波特率

两侧速率必须一致。设备还是 9600、想改成 115200 时，**不要直接改下拉框重连**（设备还在旧速率上，必然失联），正确顺序：

```
连接中把下拉框切到 115200 → 点「同步设备波特率」
   → 上位机在旧链路上发 BAUD,115200
   → 设备回 +OK,BAUD 并切换
   → 上位机自动用新速率重连
```

2 秒内没收到确认，下拉框会自动退回原速率，不会把链路弄断。

## 常见问题

| 现象 | 处理 |
|---|---|
| 下拉框没有串口 | 蓝牙没配对成功，或 CH340 驱动没装 |
| 连上但只有 `> PING` 没有回 | 波特率不一致 / PA9、PA10 接反 / 模块没上电 |
| 状态栏变黄「已连接，无应答」 | 同上，1.5 秒内没收到 `+PONG` |
| `SERVO` 返回 `-ERR,LOCKED` | 先发 `LOCK,0` 解锁 |
| 陀螺仪面板不动 | 需勾选「周期遥测」，且固件要支持 `$D` 的 11 字段 |

## 自己打包 exe

```bat
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --icon app.ico ^
  --collect-all serial ^
  --add-binary "%CONDA_PREFIX%\Library\bin\tcl90.dll;." ^
  --add-binary "%CONDA_PREFIX%\Library\bin\tcl9tk90.dll;." ^
  --add-binary "%CONDA_PREFIX%\Library\bin\zlib1.dll;." ^
  --name STM32Host host_monitor.py
```

> 用 conda 环境打包时，`_tkinter.pyd` 依赖的 `tcl90.dll` / `tcl9tk90.dll` 不会被自动收集，
> 必须手动 `--add-binary`，否则运行时报 `DLL load failed while importing _tkinter`。

## 许可

仅供学习参考。
