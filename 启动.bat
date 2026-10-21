@echo off
rem One-click start: opens a native window (WebView2). No console is kept.
rem Errors are reported with a GUI message box by tools\desktop.py.
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
if exist "D:\py310\pythonw.exe" set "PY=D:\py310\pythonw.exe"
if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
if not defined PY if exist "D:\miniconda\pythonw.exe" set "PY=D:\miniconda\pythonw.exe"
if not defined PY for %%I in (pythonw.exe) do if not "%%~$PATH:I"=="" set "PY=%%~$PATH:I"

if not defined PY (
  echo [ERROR] Python not found.
  echo Install Python 3.10 and run:  pip install -r backend\requirements.txt
  pause
  exit /b 1
)

start "" "%PY%" "tools\desktop.py" %*
exit /b 0
