@echo off
title Antigravity Hub
cd /d "%~dp0"

if not exist "hub.config.json" (
    copy "hub.config.example.json" "hub.config.json" >nul
)

start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i=0; $i -lt 30; $i++) { try { Invoke-RestMethod 'http://127.0.0.1:8787/api/inventory' -TimeoutSec 2 | Out-Null; Start-Process 'http://127.0.0.1:8787'; break } catch {}; Start-Sleep -Milliseconds 500 }"
python server.py
pause
