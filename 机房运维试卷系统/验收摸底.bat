@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ZJLab Import Probe
set "PYTHONIOENCODING=utf-8"

rem Default folder is ..\samples. You can also drag a folder onto this bat.
set "DIR=%~dp0..\待验收样例"
if not "%~1"=="" set "DIR=%~1"

set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py"
)
if not defined PY (
    for %%p in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
                "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
                "C:\Python313\python.exe" "C:\Python312\python.exe"
                "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
                "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.14\python.exe") do (
        if not defined PY if exist %%p set "PY=%%p"
    )
)
if not defined PY (
    echo.
    echo Python 3 not found. Install Python 3.10+ from python.org
    echo and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b
)

echo Scanning: %DIR%
echo.
"%PY%" "%~dp0..\tools\probe_import.py" "%DIR%" --open
echo.
echo Nothing was written to the question bank.
pause
