@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Stop ZJLab Exam System

set FOUND=0
for %%p in (8765 8766 8767 8768 8769 8770 8771 8772 8773 8774) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%p " ^| findstr "LISTENING"') do (
        taskkill /PID %%a /F >nul 2>&1
        if not errorlevel 1 (
            echo Stopped service on port %%p
            set FOUND=1
        )
    )
)

if "!FOUND!"=="0" (
    echo No running service found. Data is already safe.
)

echo.
echo Data files are kept in the data folder.
timeout /t 2 >nul
