$ErrorActionPreference = 'Stop'
$desktopRoot = $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Host '未找到 Python。请先安装 Python 3.11+，再运行：python -m pip install -r requirements.txt' -ForegroundColor Yellow
    Read-Host '按回车关闭'
    exit 1
}

& $pythonCommand.Source -c "import PySide6" 2>$null
if ($LASTEXITCODE -eq 0) {
    & $pythonCommand.Source (Join-Path $desktopRoot 'main.py')
} else {
    Write-Host '当前使用内置原生桌面窗口运行库；不需要浏览器或额外安装。' -ForegroundColor Cyan
    & $pythonCommand.Source (Join-Path $desktopRoot 'tk_desktop.py')
}
