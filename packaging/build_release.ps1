param(
    [string]$Version = "0.7.0",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = if ($PythonExe) { (Resolve-Path -LiteralPath $PythonExe).Path } else { (Get-Command python -ErrorAction Stop).Source }
$releaseRoot = Join-Path $projectRoot (".release_work\public-v{0}-{1}" -f $Version, (Get-Date -Format "yyyyMMdd"))
$distPath = Join-Path $releaseRoot "dist"
$workPath = Join-Path $releaseRoot "build"
$packagePath = Join-Path $releaseRoot "package\GeoInventory-v$Version-Windows-x64"
$zipPath = Join-Path $releaseRoot "GeoInventory-v$Version-Windows-x64.zip"
$checksumPath = Join-Path $releaseRoot "SHA256SUMS.txt"

if (Test-Path -LiteralPath $releaseRoot) {
    throw "Release output already exists: $releaseRoot"
}

New-Item -ItemType Directory -Path $releaseRoot, $packagePath | Out-Null
& $python -m PyInstaller --noconfirm --clean --distpath $distPath --workpath $workPath (Join-Path $PSScriptRoot "GeoInventory.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $distPath "GeoInventory") -Destination (Join-Path $packagePath "GeoInventory") -Recurse
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "启动地数镜.bat") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "关闭地数镜.bat") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "README_快速开始.txt") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "版本信息.txt") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination (Join-Path $packagePath "README.en.md")
Copy-Item -LiteralPath (Join-Path $projectRoot "README.zh-CN.md") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $projectRoot "NOTICE.md") -Destination $packagePath
Copy-Item -LiteralPath (Join-Path $projectRoot "CITATION.cff") -Destination $packagePath

Compress-Archive -LiteralPath $packagePath -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash *$(Split-Path $zipPath -Leaf)" | Set-Content -LiteralPath $checksumPath -Encoding ascii

[pscustomobject]@{
    Version = $Version
    Package = $zipPath
    Sha256 = $hash
    ChecksumFile = $checksumPath
} | Format-List
