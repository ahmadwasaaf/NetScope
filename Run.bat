@echo off
setlocal
cd /d "%~dp0"
title NetScope

rem ---- find Python (3.12 preferred) ----
set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo  Python was not found. Install it from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

rem ---- install libraries once ----
%PY% -c "import PySide6, tplinkrouterc6u, paramiko, keyring" >nul 2>&1
if errorlevel 1 (
  echo  Installing required libraries ^(first time only^)...
  %PY% -m pip install --upgrade pip >nul
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Library installation failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

rem ---- start NetScope without a console window ----
for /f "usebackq delims=" %%i in (`%PY% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"`) do set "PYW=%%i"
if exist "%PYW%" (
  start "" "%PYW%" main.py
) else (
  %PY% main.py
  if errorlevel 1 pause
)
endlocal
