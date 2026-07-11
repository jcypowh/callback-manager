@echo off
cd /d "%~dp0"
title Callback Manager Server
echo Starting Callback Manager...
start "Callback Manager Server" cmd /k python app.py
timeout /t 2 /nobreak >nul
start "" http://localhost:5010
