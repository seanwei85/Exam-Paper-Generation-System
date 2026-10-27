@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title ZJLab Exam System

rem ---------------------------------------------------------------------
rem If a port is already listening, do NOT just open the browser and exit.
rem The listening process may still be running the OLD code: the page you
rem see is ui.html read from disk (always the new one), while the backend
rem lives in the memory of an old process. Old backend + new page is what
rem shows up as "not found" when clicking a newly added button.
rem So ask the backend /api/version?plain=1 first: "fresh" means it matches
rem the files on disk; "stale", or no answer at all (a build old enough to
rem not have this route), means it is time to restart it.
rem ---------------------------------------------------------------------
for %%p in (8765 8766 8767 8768 8769) do (
    netstat -ano | findstr ":%%p " | findstr "LISTENING" >nul 2>&1
    if not errorlevel 1 (
        set "PORT=%%p"
        goto :port_busy
    )
)
goto :launch

:port_busy
set "ST="
for /f "usebackq delims=" %%s in (`curl -s -m 4 "http://127.0.0.1:!PORT!/api/version?plain=1" 2^>nul`) do set "ST=%%s"
if /i "!ST!"=="fresh" (
    start "" "http://127.0.0.1:!PORT!/"
    exit /b
)

rem Which process is holding the port? Only kill it if it is Python, i.e.
rem this program. Some other software on that port is none of our business.
set "LPID="
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":!PORT! " ^| findstr "LISTENING"') do set "LPID=%%i"
if not defined LPID goto :launch
set "LNAME="
for /f "tokens=1" %%n in ('tasklist /fi "PID eq !LPID!" /fo table /nh 2^>nul') do set "LNAME=%%n"
echo !LNAME! | findstr /i "python" >nul || (
    start "" "http://127.0.0.1:!PORT!/"
    exit /b
)

echo.
echo   Code has changed since this service was started.
echo   Restarting it on port !PORT! ... your questions and papers are kept.
echo.
taskkill /f /pid !LPID! >nul 2>&1
timeout /t 2 >nul
goto :launch

:launch
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"

if defined PYW (
    start "" "%PYW%" "%~dp0server.py"
    exit /b
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0server.py"
    exit /b
)

if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" "%~dp0server.py"
    exit /b
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0server.py"
    exit /b
)
if exist "C:/Python313/pythonw.exe" (
    start "" "C:/Python313/pythonw.exe" "%~dp0server.py"
    exit /b
)
if exist "C:/Python312/pythonw.exe" (
    start "" "C:/Python312/pythonw.exe" "%~dp0server.py"
    exit /b
)
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" (
    start "" "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe" "%~dp0server.py"
    exit /b
)
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.14\pythonw.exe" (
    start "" "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.14\pythonw.exe" "%~dp0server.py"
    exit /b
)

echo.
echo Python 3 not found on this computer.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
echo.
pause
