@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM  FlawScope public tunnel via cloudflared
REM
REM  Target is the React frontend on 5173, NOT the deprecated
REM  Streamlit app on 8501. Vite proxies /api to the backend on
REM  8000, so tunnelling 5173 gives a working end-to-end app.
REM
REM  IMPORTANT: keep this file ASCII-only and CRLF-terminated.
REM  Non-ASCII bytes here get misparsed by cmd.exe.
REM ============================================================

echo ============================================
echo  FlawScope - public access (cloudflared)
echo.
echo  PREREQUISITE: the app must already be running.
echo    Start it with the all-in-one launcher .bat in this folder,
echo    or manually:
echo      backend :  venv\Scripts\python.exe -m uvicorn api:app --port 8000
echo      frontend:  cd web ^&^& npm run dev        (port 5173)
echo.
echo  After about 10 seconds, look for a line like this below:
echo    https://xxxxx.trycloudflare.com
echo  Send that URL to anyone - they can open the system.
echo.
echo  Keep this window open. Closing it, or shutting down the
echo  machine, kills the link.
echo ============================================
echo.

"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:5173 --no-autoupdate

echo.
echo Tunnel closed.
pause
