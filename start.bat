@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  无痕视频下载 - 本地服务
echo  启动后浏览器将自动打开 http://127.0.0.1:8000
echo  关闭此窗口即可停止服务
echo ============================================

set "PY=python"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import fastapi" >nul 2>nul
  if not errorlevel 1 set "PY=.venv\Scripts\python.exe"
)
if "%VG_HOST%"=="" set "VG_HOST=127.0.0.1"
if "%VG_PORT%"=="" set "VG_PORT=8000"

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%VG_PORT%'"
"%PY%" -m uvicorn backend.app:app --host %VG_HOST% --port %VG_PORT%
pause
