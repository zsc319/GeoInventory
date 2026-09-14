@echo off
chcp 65001 >nul
set "PIDFILE=%LOCALAPPDATA%\GeoInventory\data\GeoInventory.pid"
if not exist "%PIDFILE%" (
  echo 未发现正在运行的地数镜服务。
  pause
  exit /b 0
)
set /p GEOPID=<"%PIDFILE%"
echo %GEOPID%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 (
  echo PID 文件内容无效，已取消停止操作。
  pause
  exit /b 1
)
tasklist /fi "PID eq %GEOPID%" | findstr /r /c:"[ ]%GEOPID%[ ]" >nul
if errorlevel 1 (
  del /q "%PIDFILE%" >nul 2>&1
  echo 服务已经停止，已清理过期 PID 文件。
  pause
  exit /b 0
)
taskkill /pid %GEOPID% /f >nul
if errorlevel 1 (
  echo 无法停止进程 %GEOPID%，请关闭地数镜命令窗口或在任务管理器中结束 GeoInventory.exe。
  pause
  exit /b 1
)
del /q "%PIDFILE%" >nul 2>&1
echo 地数镜服务已停止。
pause
