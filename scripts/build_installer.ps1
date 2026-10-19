param(
    [string]$Version = '1.1.2',
    [string]$Compiler = '',
    [string]$ReleaseDir = ''
)

$ErrorActionPreference = 'Stop'
$installerRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must have three numeric components.' }
$releasePath = $ReleaseDir
if (-not $releasePath) {
    $releasePath = Join-Path $installerRoot ("dist-sync-$Version/Omini工具箱")
    if (-not (Test-Path -LiteralPath $releasePath)) {
        $releasePath = Join-Path $installerRoot 'dist/Omini工具箱'
    }
}
$releasePath = [System.IO.Path]::GetFullPath($releasePath)
if (-not (Test-Path -LiteralPath (Join-Path $releasePath 'tools/syncthing.exe'))) {
    throw "Complete release is missing: $releasePath"
}
if (-not $Compiler) {
    $compilerCandidates = @(
        (Join-Path $installerRoot '.cache/installer-tools/inno-compiler/ISCC.exe'),
        (Join-Path $installerRoot '.cache/installer-tools/inno-setup/ISCC.exe'),
        "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe",
        "$env:ProgramFiles/Inno Setup 6/ISCC.exe",
        "$env:LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"
    )
    $Compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $Compiler) { throw 'Install Inno Setup 6.7.3+ or pass -Compiler with the path to ISCC.exe.' }
& $Compiler '/Qp' "/DAppVersion=$Version" "/DReleaseDir=$releasePath" (Join-Path $installerRoot 'installer/OminiToolbox.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$setupFile = Join-Path $installerRoot "dist-installer-$Version/Omini工具箱-$Version-Setup.exe"
$checksum = Get-FileHash -LiteralPath $setupFile -Algorithm SHA256
$checksum.Hash.ToLower() + '  ' + (Split-Path -Leaf $setupFile) | Set-Content -LiteralPath ($setupFile + '.sha256') -Encoding utf8
Write-Output "Installer ready: $setupFile"
