# STM32 Host 上位机 v1.9.0

发布日期：2026-10-31

这是首个提供 **Windows 一键安装包** 的正式版本。安装包内置 Python 运行时，目标电脑无需安装
Python、pyserial 或 Visual C++ 运行库。

## 安装包

- `STM32Host-Setup-1.9.0.exe`（约 13 MB）
- 下载地址：<https://github.com/b-as-h/Host_computer-py/releases/download/v1.9.0/STM32Host-Setup-1.9.0.exe>

### 安装步骤

1. 下载并双击 `STM32Host-Setup-1.9.0.exe`。
2. 安装包未做代码签名，SmartScreen 可能提示“未知发布者”，点 **更多信息 → 仍要运行**。
3. 默认安装到 `%LOCALAPPDATA%\Programs\STM32Host`，按当前用户安装，不需要管理员权限。
4. 可保留“创建桌面快捷方式”，完成后可直接启动。
5. 卸载：Windows **设置 → 应用 → 已安装的应用 → STM32 Host 上位机 → 卸载**。

安装包使用 Inno Setup 7.1.1 制作，包含中文安装向导、开始菜单/桌面快捷方式、
“应用和功能”卸载项；卸载会删除程序文件和快捷方式。

## 本次更新

- 新增 Inno Setup 7 一键安装包与可复现构建脚本 `installer/build_installer.bat`。
- 主程序 exe 写入 1.9.0 文件版本与产品版本信息。
- 新增 `requirements.txt`、`requirements-dev.txt`，明确运行与打包依赖。
- 清理 `__pycache__`、旧构建临时目录和误建空文件，不改变运行逻辑。
- README 增加安装、卸载、构建、SmartScreen 和系统要求说明。
- 纳入 v1.7 / v1.8 已完成的 3D 姿态仪、前台系统测试台和长时可视化验证。

## 功能概览

- 蓝牙串口 / USB-TTL 连接，支持 9600–115200 波特率与设备速率同步。
- 传感器数据、三轴加速度、温度历史曲线。
- 深色低饱和 3D 姿态仪：立方体随 `roll` / `pitch` 旋转，30fps 时间缓动，
  支持 `LIVE / WAIT / HOLD / DATA ERR / OFFLINE` 状态。
- 半圆雷达扫描、回波点、角度和距离读数。
- 舵机角度、锁定、周期遥测、雷达模式、电脑性能推送。
- HEX 发送、定时发送、命令历史、时间戳和原始日志。
- Per-Monitor V2 DPI 感知，适配 1080p / 2K 与 125% / 150% 缩放。

## 验证结果

| 项目 | 结果 |
|---|---|
| 协议单元自检 | 79 项通过，0 失败 |
| 前台 E2E 系统测试 | 55 项通过，0 失败 |
| 3D 陀螺仪长时展示 | 6.40 秒，53 帧，状态迁移全部通过 |
| 温度曲线长时展示 | 5.61 秒，13 帧，坏帧未写入 |
| 雷达长时展示 | 5.62 秒，105 帧，96 种角度 |
| 安装包静默安装 | 通过（自定义目录、文件哈希、版本号一致） |
| 快捷方式 | 开始菜单与桌面快捷方式创建/删除通过 |
| 卸载 | 程序目录、快捷方式、HKCU 卸载项全部清除 |
| 安装版启动 | 窗口标题正确，DPI 感知正常 |
| 源码语法检查 | `host_monitor.py`、`selftest.py`、`sysinfo.py`、`tools/*.py` 全部通过 |

详细测试报告：<https://github.com/b-as-h/Host_computer-py/blob/main/docs/system_test_report.md>

## 文件校验（SHA-256）

```text
1f9a76b68bd232b8277c0239eeec79b7a3f268727ac3ceaf66a22c8db3323c0c  STM32Host-Setup-1.9.0.exe
b73cdb43315d1ed2e5676720a6bcbd5975aa2bb602f6735919d8dfba92649aee  STM32Host.exe
```

## 系统要求

- Windows 10 / 11 64 位
- 约 60 MB 磁盘空间
- 蓝牙模块已配对，或 USB-TTL 直连开发板
- 源码运行另需 Python 3.9+ 64 位（含 tkinter）

## 已知问题与限制

- **安装包未签名**：SmartScreen 会提示未知发布者；正式分发前建议购买代码签名证书。
- **仅支持 x64**：32 位 Windows 无法安装。
- **未做真实硬件回归**：串口、蓝牙、CH340 驱动、供电和线缆质量需在目标开发板上复测；
  自动化测试使用虚拟串口与固定虚拟数据。
- **无双击自动更新**：升级需下载新安装包覆盖安装，安装目录和快捷方式会保留。
- **杀毒软件误报**：PyInstaller 单文件程序可能被个别安全软件误判，可将安装目录加入信任。
- 当前安装包按用户安装；如需全机器安装，可在后续版本增加管理员安装模式。
