@echo off
chcp 936 >nul
title 兼职匹配工作台
cd /d "%~dp0"

netstat -ano | findstr "127.0.0.1:8765" | findstr "LISTENING" >nul
if not errorlevel 1 (
  start "" "http://127.0.0.1:8765/"
  exit /b
)

echo.
echo   兼职匹配工作台 正在启动，页面几秒后会自动打开...
echo   平台：BOSS直聘 / 58同城 / 实习僧 / 电鸭社区
echo   要关掉服务：任务管理器里结束 pythonw.exe。
echo.

where pythonw >nul 2>&1
if errorlevel 1 goto trypy
start "" pythonw job_bridge.py
goto wait

:trypy
where python >nul 2>&1
if errorlevel 1 goto nopy
start "" python job_bridge.py
goto wait

:nopy
echo   [x] 没找到 Python。
echo.
echo   装一个 Python 3.8 或更高版本：https://www.python.org/downloads/
echo   安装时记得勾选 "Add Python to PATH"，装完再双击本文件。
echo.
pause
exit /b

:wait
timeout /t 6 >nul
netstat -ano | findstr "127.0.0.1:8765" | findstr "LISTENING" >nul
if errorlevel 1 goto fail
rem 服务已起来，页面由它自己打开，这里不再多开一个标签
exit /b

:fail
echo.
echo   [x] 服务没起来：可能端口 8765 被占用，或 job_bridge.py 不在同目录。
echo       手动排查：在本窗口执行  python job_bridge.py  看报错。
echo.
rem 兜底再开一次页面，至少让人看到东西
start "" "http://127.0.0.1:8765/"
pause
