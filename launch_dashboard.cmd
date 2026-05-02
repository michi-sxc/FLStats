@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_flp_dashboard.ps1"
if errorlevel 1 (
  echo.
  echo FLP Observatory launcher failed.
  pause
)
