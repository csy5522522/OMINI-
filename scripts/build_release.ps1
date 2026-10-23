param(
    [string]$DistDir = 'dist-release',
    [string]$WorkDir = 'build-release'
)

$ErrorActionPreference = 'Stop'
$releaseBuildRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $releaseBuildRoot
$releaseAppName = 'Omini' + [char]0x5DE5 + [char]0x5177 + [char]0x7BB1
$releaseSpec = Join-Path $releaseBuildRoot ($releaseAppName + '.spec')
& py -3.11 -c 'import socks'
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt (including requests[socks]) before building.' }
if (-not (Test-Path -LiteralPath (Join-Path $releaseBuildRoot 'tools/cli-proxy-api.exe'))) {
    & py -3.11 (Join-Path $releaseBuildRoot 'scripts/fetch_cliproxyapi.py')
    if ($LASTEXITCODE -ne 0) { throw 'Verified CLIProxyAPI engine download failed.' }
}
& py -3.11 -m PyInstaller --noconfirm --distpath $DistDir --workpath $WorkDir $releaseSpec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
$releaseRelease = Join-Path $DistDir $releaseAppName
& py -3.11 (Join-Path $releaseBuildRoot 'release_packaging.py') --copy-tools (Join-Path $releaseRelease 'tools')
if ($LASTEXITCODE -ne 0) { throw 'Engine packaging failed.' }
Copy-Item -LiteralPath (Join-Path $releaseBuildRoot 'README.md') -Destination (Join-Path $releaseRelease 'README.md') -Force
Write-Output ('Built: ' + (Join-Path $releaseBuildRoot (Join-Path $releaseRelease ($releaseAppName + '.exe'))))
