@echo off
rem ============================================================
rem  ETF Dashboard launcher - double click to start
rem  Starts the local server (http://127.0.0.1:8899) with the
rem  web-page rerun button enabled, then keeps the window open.
rem ============================================================
setlocal
cd /d "%~dp0fund-strategy"

set PY=
if exist "C:\Users\Chen\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\Chen\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY set "PY=python"

echo Starting ETF dashboard with: %PY%
echo Close this window to stop the server.
echo.
"%PY%" etf_dashboard.py --serve

echo.
echo Server stopped.
pause
