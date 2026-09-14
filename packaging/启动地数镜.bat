@echo off
chcp 65001 >nul
set "APP=%~dp0GeoInventory\GeoInventory.exe"
if not exist "%APP%" (
  echo 未找到 GeoInventory.exe，请保持发行包目录结构不变。
  pause
  exit /b 1
)
start "地数镜 GeoInventory" "%APP%"
exit /b 0
