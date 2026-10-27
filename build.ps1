param(
    [string]$Python = "py -3.11"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$appName = 'Omini' + [char]0x5DE5 + [char]0x5177 + [char]0x7BB1
Set-Location $root

function Invoke-Python {
    param([string[]]$Arguments)
    & py -3.11 @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed: $Arguments" }
}

function Copy-CommandBinary {
    param([string]$Name, [string]$Destination)
    $destinationPath = Join-Path $root $Destination
    if (Test-Path -LiteralPath $destinationPath) { return }
    $command = Get-Command $Name -ErrorAction Stop
    $source = $command.Source
    if (-not (Test-Path -LiteralPath $source)) { throw "Cannot locate $Name binary" }
    Copy-Item -LiteralPath $source -Destination $destinationPath -Force
}

Invoke-Python @('-m', 'pip', 'install', '-r', 'requirements.txt')
Invoke-Python @('scripts/fetch_cliproxyapi.py')
$engineBuildRoot = Join-Path $root '.cache/engine-build'
New-Item -ItemType Directory -Path $engineBuildRoot -Force | Out-Null
New-Item -ItemType Directory -Path 'tools' -Force | Out-Null

Invoke-WebRequest 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile 'tools/yt-dlp.exe'
Copy-CommandBinary 'aria2c.exe' 'tools/aria2c.exe'

# gallery-dl and spotdl do not ship portable Windows binaries. Create them from
# their installed Python entry points so the final application has no Python requirement.
@'
from gallery_dl import main
main()
'@ | Set-Content -LiteralPath (Join-Path $engineBuildRoot 'gallery_dl_runner.py') -Encoding utf8
@'
from spotdl.console.entry_point import console_entry_point
console_entry_point()
'@ | Set-Content -LiteralPath (Join-Path $engineBuildRoot 'spotdl_runner.py') -Encoding utf8
Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--distpath', (Join-Path $engineBuildRoot 'dist'), '--workpath', (Join-Path $engineBuildRoot 'build'), '--specpath', $engineBuildRoot, '--name', 'gallery-dl', (Join-Path $engineBuildRoot 'gallery_dl_runner.py'))
Copy-Item -LiteralPath (Join-Path $engineBuildRoot 'dist/gallery-dl.exe') -Destination 'tools/gallery-dl.exe' -Force
Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--collect-data', 'pykakasi', '--distpath', (Join-Path $engineBuildRoot 'dist'), '--workpath', (Join-Path $engineBuildRoot 'build'), '--specpath', $engineBuildRoot, '--name', 'spotdl', (Join-Path $engineBuildRoot 'spotdl_runner.py'))
Copy-Item -LiteralPath (Join-Path $engineBuildRoot 'dist/spotdl.exe') -Destination 'tools/spotdl.exe' -Force

& (Join-Path $root 'scripts/build_release.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
