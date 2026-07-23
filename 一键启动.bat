@echo off
setlocal
cd /d "%~dp0"

echo [gptimage] starting WSL proxy stack + backend + frontend...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1" -WithFrontend -AllowDockerStart -RequireProxyStack
if errorlevel 1 (
  echo.
  echo [gptimage] start failed. See messages above.
  if /I not "%~1"=="nopause" pause
  exit /b 1
)

echo.
echo [gptimage] frontend: http://127.0.0.1:3000/
echo [gptimage] backend:  http://127.0.0.1:8000/
echo   proxy=40080
echo.
if /I not "%~1"=="nopause" pause
exit /b 0
endlocal
