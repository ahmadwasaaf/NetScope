@echo off
setlocal
cd /d "%~dp0"
title NetScope - build installer

rem ---- 1) NetScope.exe (same steps as Build.bat) ----
call "%~dp0Build.bat" nopause
if not exist "dist\NetScope.exe" (
  echo  NetScope.exe was not built - see the messages above.
  pause
  exit /b 1
)

rem ---- 2) Inno Setup compiler (installed automatically with winget if missing) ----
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo  Installing Inno Setup ^(free installer builder^) with winget...
  winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
  set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  if not exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
)
if not exist "%ISCC%" (
  echo.
  echo  Inno Setup was not found. Install it from https://jrsoftware.org/isdl.php and run this again.
  pause
  exit /b 1
)

rem ---- 3) NetScope-Setup.exe ----
"%ISCC%" "installer\NetScope.iss"
if errorlevel 1 ( echo  Installer build failed. & pause & exit /b 1 )
copy /y "dist\NetScope-Setup.exe" "NetScope-Setup.exe" >nul
echo.
echo  Done!  NetScope-Setup.exe is ready in this folder - share it or double-click to install.
explorer /select,"%~dp0NetScope-Setup.exe"
pause
endlocal
