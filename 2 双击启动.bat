@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title EasyController

set "APP=app"
set "ENTRY=%APP%\run_sticklab.py"
set "REQ=%APP%\requirements.txt"

echo ============================================================
echo                      EasyController
echo ============================================================
echo.

if not exist "%ENTRY%" (
    echo [ERROR] app\run_sticklab.py not found.
    echo Please re-extract the whole package.
    echo.
    pause
    exit /b 1
)

rem ---------- 1. pick a python that has the deps ----------
set "PY="
for %%V in (3.11 3.12 3.10) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    py -3 -c "import flask, pygame, vgamepad, usb, libusb_package, hid" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python -c "import flask, pygame, vgamepad, usb, libusb_package, hid" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo [ERROR] Python not found.
    echo.
    echo Please send "1 DSH.txt" to DSH first, it will install everything.
    echo.
    pause
    exit /b 1
)

echo [1/3] Python: %PY%
%PY% -c "import sys; print('      version', sys.version.split()[0])"

rem ---------- 2. dependencies ----------
echo [2/3] checking dependencies...
%PY% -c "import flask, pygame, vgamepad, usb, libusb_package, hid" >nul 2>&1
if errorlevel 1 (
    echo       installing, please wait...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r "%REQ%"
    if errorlevel 1 (
        echo.
        echo [ERROR] install failed.
        echo Try manually:  %PY% -m pip install -r "%REQ%"
        echo.
        pause
        exit /b 1
    )
)
echo       ok

rem ---------- 3. run ----------
echo [3/3] starting service...
echo.
echo ------------------------------------------------------------
echo   The browser will open automatically.
echo   Keep this window open. Close it to stop the service.
echo ------------------------------------------------------------
echo.

%PY% "%ENTRY%"

echo.
echo service stopped.
pause
