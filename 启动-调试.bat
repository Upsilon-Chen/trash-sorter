@echo off
rem Debug start: keeps the console open and prints logs (use this when something fails).
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "D:\miniconda\python.exe" set "PY=D:\miniconda\python.exe"
if not defined PY for %%I in (python.exe) do if not "%%~$PATH:I"=="" set "PY=%%~$PATH:I"

if not defined PY (
  echo [ERROR] Python not found.
  pause
  exit /b 1
)

echo Using interpreter: %PY%
echo Logs: logs\desktop.log
echo.
"%PY%" "tools\desktop.py" %*
echo.
echo Exit code: %ERRORLEVEL%
pause
