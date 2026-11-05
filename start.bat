@echo off
title Screengu - Unified Stock Screener
cd /d "%~dp0"
setlocal

rem ============================================================
rem  Requires Python 3.10+ (3.11 recommended).
rem  Why: app/core/sync.py uses PEP 604 annotations (str | None)
rem  without "from __future__ import annotations"; that raises
rem  TypeError at import time on Python 3.9, so the app cannot
rem  start at all. Failing here with a clear message beats a
rem  confusing traceback three seconds later.
rem ============================================================

set "PORT=8765"
set "MINPY=3.10"

rem ---------- find a python that can actually run this app ----------
rem A candidate passes only if BOTH hold:
rem   1) sys.version_info >= (3,10)
rem   2) every hard dependency imports
rem The old gate checked only 6 of them, so a machine that had
rem uvicorn+pandas but no jieba / py_mini_racer / curl_cffi / tqdm
rem passed the gate and then failed later at runtime.
set "PYEXE="
set "OLDEXE="
for %%P in ("python" "py -3" "C:\Python311\python.exe" "C:\Python312\python.exe" "C:\Python313\python.exe") do (
  if not defined PYEXE (
    %%~P -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
    if errorlevel 1 (
      %%~P -c "import sys" >nul 2>&1
      if not errorlevel 1 set "OLDEXE=%%~P"
    ) else (
      %%~P -c "import uvicorn, fastapi, pandas, numpy, requests, akshare, jieba, py_mini_racer, curl_cffi, tqdm" >nul 2>&1
      if not errorlevel 1 set "PYEXE=%%~P"
    )
  )
)

rem ---------- first run: no ready-to-use python ----------
if not defined PYEXE (
  if defined OLDEXE (
    echo [ERROR] Found Python at "%OLDEXE%" but it is OLDER than %MINPY%.
    echo         This app requires Python %MINPY% or newer.
    echo.
    echo         Install from https://www.python.org/downloads/
    echo         and tick "Add Python to PATH" during setup, then retry.
    echo.
    pause
    exit /b 1
  )
  echo [SETUP] No Python with the required packages was found.
  echo         Trying to install dependencies automatically, 1-3 min...
  echo.
  where py >nul 2>&1
  if not errorlevel 1 (set "PYEXE=py -3") else (set "PYEXE=python")

  %PYEXE% -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python %MINPY% or newer was not found on this machine.
    echo.
    echo         Download it from https://www.python.org/downloads/
    echo         and tick "Add Python to PATH" during setup, then retry.
    echo.
    pause
    exit /b 1
  )

  %PYEXE% -m pip install -r requirements.txt
  %PYEXE% -c "import uvicorn, fastapi, pandas, numpy, requests, akshare, jieba, py_mini_racer, curl_cffi, tqdm" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo [ERROR] Auto setup failed. Please run manually then retry:
    echo         %PYEXE% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  echo.
  echo [SETUP] Dependencies installed OK.
)

rem ---------- check if port is already in use ----------
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" > nul
if %errorlevel%==0 (
  echo Port %PORT% is already in use, opening browser...
  start "" "http://127.0.0.1:%PORT%/"
  exit /b 0
)

rem ---------- launch server in a NEW independent window ----------
rem Server runs run_server.bat: on crash the window STAYS OPEN so you can
rem read the error; it also pauses on normal exit. This prevents the
rem "cmd window vanished by itself and the app died" symptom.
echo Starting server (new window), please wait...
start "Screengu-Server" cmd /c "run_server.bat"

rem ---------- wait until server is ready (max 40s) ----------
set /a tries=0
:waitloop
timeout /t 1 /nobreak > nul
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" > nul
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% geq 40 goto fail
goto waitloop

:ready
echo Server is up.
start "" "http://127.0.0.1:%PORT%/"
echo.
echo ===============================================
echo   Server is running, browser opened:
echo   http://127.0.0.1:%PORT%/
echo   Closing THIS window will NOT stop the server.
echo   To stop: close the black window titled
echo   "Screengu-Server".
echo ===============================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Server failed to start or timed out.
echo         First run may need: "%PYEXE%" -m pip install -r requirements.txt
echo         Or run manually:
echo         "%PYEXE%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
pause
exit /b 1
