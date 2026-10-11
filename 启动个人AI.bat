@echo off
cd /d "%~dp0"
title Personal AI

set "LOG=%LOCALAPPDATA%\personal_ai\launch.log"
if not exist "%LOCALAPPDATA%\personal_ai" mkdir "%LOCALAPPDATA%\personal_ai" >nul 2>nul
call :log "===== 双击启动 ====="

echo.
echo  ============================================
echo     Personal AI   一键启动
echo  ============================================
echo.

netstat -ano | findstr ":8787" | findstr "LISTENING" >nul
if not errorlevel 1 goto ALREADY

echo  [1/3] 检查 AI 网关 OmniRoute ...
call :log "步骤1: 端口 8787 未被占用"
netstat -ano | findstr ":20128" >nul
if not errorlevel 1 goto GW_READY
call :log "步骤2: 端口 20128 已监听, 跳过网关启动"

where omniroute >nul 2>nul
if errorlevel 1 goto GW_SKIP
call :log "步骤3: 找到 omniroute, 开始后台启动网关"

echo        正在后台启动网关, 首次需要几十秒, 请勿重复双击.
echo        等待就绪
omniroute serve --no-open --daemon
call :log "步骤4: 网关启动命令已返回"

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
call :log "步骤5: 网关等待超时"
goto GW_OK

:GW_SKIP
echo        没找到 omniroute 命令, 跳过网关.
call :log "步骤5: 没找到 omniroute 命令"
goto GW_OK

:GW_READY
echo.
echo        网关已就绪.
call :log "步骤5: 网关已就绪"

:GW_OK
echo  [2/3] 启动 Personal AI ...
call :log "步骤6: 准备启动 python"

rem 用 PATH 上的 python，不写死任何一台机器的绝对路径
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY=py"
    ) else (
        echo.
        echo  没有找到 python. 请先安装 Python 3.9 以上版本,
        echo  并勾选 Add Python to PATH.
        echo.
        call :log "错误: PATH 上没有 python / py"
        pause >nul
        exit /b 1
    )
)
call :log "步骤6: 使用解释器 %PY%"

echo  [3/3] 服务运行中: http://127.0.0.1:8787
echo        要停止就关掉这个窗口, 或按 Ctrl+C.
echo.
"%PY%" scripts\start_local.py
call :log "步骤7: python 进程已退出"
if errorlevel 1 (
    echo.
    echo  [提示] 第一次接入网关没成功, 10 秒后重试一次 ...
    call :log "步骤8: 进入重试分支"
    ping -n 11 127.0.0.1 >nul
    "%PY%" scripts\start_local.py
    call :log "步骤8: 重试的 python 已退出"
)
if errorlevel 1 (
    echo.
    echo  [提示] 仍接不上网关, 改用离线兜底模式.
    echo        页面能看, 但回答来自本地示例, 不是真实 AI.
    echo.
    call :log "步骤9: 进入离线兜底分支"
    "%PY%" -m personal_ai --serve
    call :log "步骤9: 离线模式的 python 已退出"
)

echo.
echo  服务已停止. 按任意键关闭.
call :log "步骤10: 已到达 pause, 脚本即将正常结束"
pause >nul
call :log "===== 正常结束 ====="
exit /b 0

:ALREADY
echo  Personal AI 已经在运行了 (端口 8787 已被占用).
echo  直接帮你打开浏览器, 不用重复启动.
echo.
start "" http://127.0.0.1:8787
echo  按任意键关闭本窗口 (不会影响正在运行的服务).
call :log "走了 ALREADY 分支, 已到达 pause"
pause >nul
call :log "===== 正常结束 (ALREADY) ====="
exit /b 0

:log
echo [%DATE% %TIME%] %*>> "%LOG%"
goto :eof
