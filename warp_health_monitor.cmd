@echo off
REM WARP health monitor - runs as a background task
REM Checks :40080 and FlareSolverr every 60 seconds, restarts the local proxy stack if unresponsive.

set "PROJECT_WSL=/mnt/d/SelfMadeTool/AutoRegister/gptimage"

:loop
curl.exe -s --max-time 10 -o nul -w "%%{http_code}" http://127.0.0.1:40080/ > "%TEMP%\warp_check.tmp"
set /p STATUS=<%TEMP%\warp_check.tmp

curl.exe -fsS --max-time 10 http://127.0.0.1:8191/ > nul
set "FLARE_STATUS=%ERRORLEVEL%"

if not "%STATUS%"=="400" goto restart_stack
if not "%FLARE_STATUS%"=="0" goto restart_stack

timeout /t 60 /nobreak > nul
goto loop

:restart_stack
echo %DATE% %TIME% Proxy stack unhealthy (proxy=%STATUS%, flaresolverr=%FLARE_STATUS%), restarting...
wsl -d HermesUbuntu -- bash "%PROJECT_WSL%/scripts/start_proxy_stack_wsl.sh" "%PROJECT_WSL%"
timeout /t 30 /nobreak > nul
goto loop
