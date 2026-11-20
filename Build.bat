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

echo.
echo  Building NetScope.exe ... this takes 1-3 minutes.
echo.
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 ( pause & exit /b 1 )

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name NetScope --icon icon.ico --add-data "icon.ico;." ^
  --collect-submodules netscope ^
  --collect-all tplinkrouterc6u ^
  --collect-submodules keyring --copy-metadata keyring ^
  --hidden-import keyring.backends.Windows ^
  main.py
if errorlevel 1 (
  echo.
  echo  Build failed - see the messages above.
  pause
  exit /b 1
)

copy /y "dist\NetScope.exe" "NetScope.exe" >nul
rmdir /s /q build >nul 2>&1
del /q NetScope.spec >nul 2>&1
echo.
echo  Done!  NetScope.exe is ready in this folder.
echo  You can copy it anywhere - it does not need Python.
echo.
if /i not "%~1"=="nopause" (
  explorer /select,"%~dp0NetScope.exe"
  pause
)
endlocal
