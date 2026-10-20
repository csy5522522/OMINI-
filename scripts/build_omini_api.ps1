param(
    [string]$DistDir = 'dist-omini-api',
    [string]$WorkDir = 'build-omini-api'
)

$ErrorActionPreference = 'Stop'
$apiBuildRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $apiBuildRoot
$apiAppName = 'Omini' + [char]0x5DE5 + [char]0x5177 + [char]0x7BB1
$apiSpec = Join-Path $apiBuildRoot ($apiAppName + '.spec')
& py -3.11 -c 'import socks'
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt (including requests[socks]) before building.' }
if (-not (Test-Path -LiteralPath (Join-Path $apiBuildRoot 'tools/cli-proxy-api.exe'))) {
    & py -3.11 (Join-Path $apiBuildRoot 'scripts/fetch_cliproxyapi.py')
    if ($LASTEXITCODE -ne 0) { throw 'Verified CLIProxyAPI engine download failed.' }
}
& py -3.11 -m PyInstaller --noconfirm --distpath $DistDir --workpath $WorkDir $apiSpec
if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
$apiRelease = Join-Path $DistDir $apiAppName
& py -3.11 (Join-Path $apiBuildRoot 'release_packaging.py') --copy-tools (Join-Path $apiRelease 'tools')
if ($LASTEXITCODE -ne 0) { throw 'Engine packaging failed.' }
Copy-Item -LiteralPath (Join-Path $apiBuildRoot 'README.md') -Destination (Join-Path $apiRelease 'README.md') -Force
Write-Output ('Built: ' + (Join-Path $apiBuildRoot (Join-Path $apiRelease ($apiAppName + '.exe'))))
