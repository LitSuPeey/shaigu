@echo off
rem ============================================================
rem  Screengu server keeper window
rem  --------------------------------------------------------
rem  Why this file exists:
rem  start.bat used to launch the server as `cmd /c uvicorn ...`.
rem  If the user accidentally clicked the window X, pressed Ctrl+C,
rem  or uvicorn itself crashed, the window DISAPPEARED instantly:
rem  no error visible, no way to recover. Symptom reported as
rem  "the cmd window randomly vanishes and the app stops".
rem
rem  This script runs the server in a titled window and:
rem    1) prints the exit reason first, then pauses (window stays)
rem    2) keeps the error visible instead of closing on crash
rem    3) rescues the sync thread: sync runs with daemon=False, so
rem       quitting waits for an in-flight sync to finish cleanly
rem
rem  Python 3.10+ is required (see start.bat header for why).
rem ============================================================
title Screengu-Server (keep open; close = stops server)
cd /d "%~dp0"

set "PORT=8765"
set "MINPY=3.10"
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
if not defined PYEXE (
  echo [ERROR] No usable Python found for the server.
  if defined OLDEXE echo         "%OLDEXE%" is older than %MINPY%.
  echo.
  echo         Run start.bat first - it can install dependencies.
  echo         If Python is missing entirely, install %MINPY%+ from
  echo         https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   Screengu server starting on http://127.0.0.1:%PORT%/
echo   Python: %PYEXE%
echo.
echo   * Keep THIS window open while using the app.
echo   * To stop the server: press Ctrl+C here, or close it.
echo   * If it crashes, the error stays on screen (no vanish).
echo ============================================================
echo.

rem NOTE: %PYEXE% is intentionally unquoted. A candidate may be the
rem launcher form "py -3"; quoting it would look for an executable
rem literally named "py -3". None of the candidates contain spaces.
%PYEXE% -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level info
set "RC=%errorlevel%"

echo.
echo ============================================================
if "%RC%"=="0" (
  echo   Server exited normally.
) else (
  echo   [WARN] Server exited with code %RC%.
  echo.
  echo   Common fixes:
  echo     - Port %PORT% busy:  netstat -ano ^| findstr :%PORT%
  echo     - Missing deps:      %PYEXE% -m pip install -r requirements.txt
  echo     - Python too old:    need %MINPY%+ (check: %PYEXE% --version)
  echo     - Read the traceback printed above.
  echo.
  echo   You can also run the server manually in this folder:
  echo     %PYEXE% -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
)
echo ============================================================
echo.
echo Window stays open so the error above remains readable.
pause
