$ErrorActionPreference = 'Stop'
$desktopRoot = Split-Path $PSScriptRoot -Parent
$projectRoot = Split-Path $desktopRoot -Parent
$python = (Get-Command python -ErrorAction Stop).Source

# 当前发行配置使用 Python 内置 Tk 原生窗口，不需要 Qt 运行库。
& $python -m pip install pyinstaller

$specPath = Join-Path $PSScriptRoot '地数镜桌面版.spec'
& $python -m PyInstaller --noconfirm --clean $specPath

$inno = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($inno) {
    & $inno.Source (Join-Path $PSScriptRoot '地数镜桌面版.iss')
    Write-Host '安装包已生成到 packaging\installer-output' -ForegroundColor Green
} else {
    Write-Host '便携版已生成到 dist\地数镜桌面版。若需安装包，请安装 Inno Setup 后再次运行本脚本。' -ForegroundColor Yellow
}
