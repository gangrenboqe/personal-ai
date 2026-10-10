@echo off
cd /d "E:\DSHPersonalAI"
title Personal AI

echo.
echo  ============================================
echo     Personal AI   一键启动
echo  ============================================
echo.

netstat -ano | findstr ":8787" | findstr "LISTENING" >nul
if not errorlevel 1 goto ALREADY

echo  [1/3] 检查 AI 网关 OmniRoute ...
netstat -ano | findstr ":20128" >nul
if not errorlevel 1 goto GW_READY

where omniroute >nul 2>nul
if errorlevel 1 goto GW_SKIP

echo        正在后台启动网关, 首次需要几十秒, 请勿重复双击.
echo        等待就绪
omniroute serve --no-open --daemon
set /a GW_N=0
:GW_WAIT
ping -n 3 127.0.0.1 >nul
netstat -ano | findstr ":20128" >nul
if not errorlevel 1 goto GW_READY
set /a GW_N+=1
if %GW_N% geq 45 goto GW_TIMEOUT
<nul set /p=.
goto GW_WAIT

:GW_TIMEOUT
echo.
echo        网关等待超时, 将以离线兜底模式运行.
goto GW_OK

:GW_SKIP
echo        没找到 omniroute 命令, 跳过网关.
goto GW_OK

:GW_READY
echo.
echo        网关已就绪.

:GW_OK
echo  [2/3] 启动 Personal AI ...
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

start "" /min cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:8787"

echo  [3/3] 服务运行中: http://127.0.0.1:8787
echo        要停止就关掉这个窗口, 或按 Ctrl+C.
echo.
"%PY%" scripts\start_local.py
if errorlevel 1 (
    echo.
    echo  [提示] 第一次接入网关没成功, 10 秒后重试一次 ...
    ping -n 11 127.0.0.1 >nul
    "%PY%" scripts\start_local.py
)
if errorlevel 1 (
    echo.
    echo  [提示] 仍接不上网关, 改用离线兜底模式.
    echo        页面能看, 但回答来自本地示例, 不是真实 AI.
    echo.
    "%PY%" -m personal_ai --serve
)

echo.
echo  服务已停止. 按任意键关闭.
pause >nul
exit /b 0

:ALREADY
echo  Personal AI 已经在运行了 (端口 8787 已被占用).
echo  直接帮你打开浏览器, 不用重复启动.
echo.
start "" http://127.0.0.1:8787
echo  按任意键关闭本窗口 (不会影响正在运行的服务).
pause >nul
exit /b 0
