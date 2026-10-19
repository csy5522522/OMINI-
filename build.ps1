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
Invoke-Python @('scripts/fetch_syncthing.py')
Remove-Item -LiteralPath 'build' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath 'dist' -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path 'tools' -Force | Out-Null

Invoke-WebRequest 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile 'tools/yt-dlp.exe'
Copy-CommandBinary 'aria2c.exe' 'tools/aria2c.exe'
Copy-CommandBinary 'ffmpeg.exe' 'tools/ffmpeg.exe'

# gallery-dl and spotdl do not ship portable Windows binaries. Create them from
# their installed Python entry points so the final application has no Python requirement.
@'
from gallery_dl import main
main()
'@ | Set-Content -LiteralPath 'gallery_dl_runner.py' -Encoding utf8
@'
from spotdl.console.entry_point import console_entry_point
console_entry_point()
'@ | Set-Content -LiteralPath 'spotdl_runner.py' -Encoding utf8
Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--name', 'gallery-dl', 'gallery_dl_runner.py')
Move-Item -LiteralPath 'dist/gallery-dl.exe' -Destination 'tools/gallery-dl.exe' -Force
Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--collect-data', 'pykakasi', '--name', 'spotdl', 'spotdl_runner.py')
Move-Item -LiteralPath 'dist/spotdl.exe' -Destination 'tools/spotdl.exe' -Force
Remove-Item -LiteralPath 'gallery_dl_runner.py','spotdl_runner.py' -Force

Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--windowed', '--runtime-hook', 'runtime_hook.py',
    '--name', $appName, '--icon', 'assets/omini_icon.ico', '--add-data', 'assets;assets', 'app.py')
$releaseDirectory = Join-Path 'dist' $appName
Copy-Item -LiteralPath 'tools' -Destination (Join-Path $releaseDirectory 'tools') -Recurse -Force
Write-Host "Built: $(Join-Path $root (Join-Path $releaseDirectory ($appName + '.exe')))"
