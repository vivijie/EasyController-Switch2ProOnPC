@echo off
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if exist "%PY%" goto run
set "PY=python"

:run
echo ============================================
echo   StickLab - Switch2Pro Controller
echo ============================================
echo   正在启动，稍后会自动打开浏览器
echo   关闭这个窗口 = 停止服务
echo ============================================
echo.
"%PY%" "%~dp0run_sticklab.py" %*
echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
