@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="

rem 依次尝试：py 启动器 -> PATH 里的 python -> conda -> 常见安装目录
for %%C in ("py -3" "python" "%USERPROFILE%\miniconda3\python.exe" "%USERPROFILE%\anaconda3\python.exe") do (
    if not defined PY (
        %%~C -c "import tkinter" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)

if not defined PY (
    echo [错误] 没有找到带 tkinter 的 Python。
    echo.
    echo 请安装 Python 3.8+ ^(python.org^)，安装时勾选 "tcl/tk and IDLE"，
    echo 然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%P in ('%PY% -c "import sys;print(sys.executable)"') do set "PYEXE=%%P"
if not defined PYEXE set "PYEXE=%PY%"

echo 使用 Python: %PYEXE%

%PY% -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo 未安装 pyserial，正在安装 ...
    %PY% -m pip install pyserial
    if errorlevel 1 (
        echo 官方源失败，改用清华镜像重试 ...
        %PY% -m pip install pyserial -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    %PY% -c "import serial" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [错误] pyserial 安装失败，请手动执行：
        echo        %PY% -m pip install pyserial
        pause
        exit /b 1
    )
)

rem 以最小化控制台启动界面，本窗口立即退出
start "BASH-STM32 Host" /min "%PYEXE%" "%~dp0host_monitor.py"
exit /b 0
