@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FlawScope

echo Preparing to start, please wait...

REM ============================================================
REM  FlawScope one-click launcher (single window)
REM
REM  All real logic lives in run_all.py; this file only locates
REM  the Python interpreter and hands over.
REM
REM  IMPORTANT: keep this file ASCII-only and CRLF-terminated.
REM    - Multi-byte chars here break cmd.exe parsing after chcp 65001.
REM    - LF-only line endings break the parenthesized if-blocks below.
REM    Both failures look like "double-clicked and nothing happened".
REM ============================================================

if not exist "venv\Scripts\python.exe" (
    echo.
    echo [ERROR] venv\Scripts\python.exe not found.
    echo         Create it first:  python -m venv venv
    echo         Then install deps: venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "run_all.py" (
    echo.
    echo [ERROR] run_all.py not found in this directory.
    echo         Keep this .bat and run_all.py in the same folder.
    echo.
    pause
    exit /b 1
)

echo.
"venv\Scripts\python.exe" "run_all.py"
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo [NOTE] Launcher exited with code %EXITCODE%. See messages above.
)

echo.
echo Press any key to close this window...
pause >nul
