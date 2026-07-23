@echo off
setlocal
cd /d "%~dp0"

echo [gptimage] stopping backend / monitor / WSL proxy stack...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_all.ps1" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo [gptimage] stop finished with warnings/errors. See messages above.
) else (
  echo [gptimage] stopped.
)

if /I not "%~1"=="nopause" pause
exit /b %ERR%
endlocal
