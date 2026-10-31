@echo off
setlocal
cd /d "%~dp0.."

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" (
  echo [ERROR] Inno Setup 7 not found. Install it from https://jrsoftware.org/isdl.php
  exit /b 1
)

echo [1/3] Install build dependencies...
py -3 -m pip install --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 exit /b 1

echo [2/3] Build STM32Host.exe...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --icon "%CD%\app.ico" ^
  --version-file "%CD%\installer\version_info.txt" ^
  --collect-all serial ^
  --name STM32Host ^
  --distpath "%CD%\dist" ^
  --workpath "%TEMP%\stm32host-build" ^
  --specpath "%TEMP%\stm32host-spec" ^
  "%CD%\host_monitor.py"
if errorlevel 1 exit /b 1

echo [3/3] Build installer...
"%ISCC%" /Qp "%CD%\installer\STM32Host.iss"
if errorlevel 1 exit /b 1

echo.
echo Done:
dir /b "%CD%\dist\STM32Host-Setup-*.exe"
endlocal
