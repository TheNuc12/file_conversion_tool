# Builds Rotary.exe and its installer on a Windows PC with Python 3.11+ (and, for the
# installer, Inno Setup 6 from https://jrsoftware.org/isdl.php). From the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# The GitHub Actions workflow (.github/workflows/windows-app.yml) does the same steps.
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

function Invoke-Checked {
    & $args[0] $args[1..($args.Count - 1)]
    if ($LASTEXITCODE -ne 0) { throw "$($args -join ' ') failed with exit code $LASTEXITCODE" }
}

Invoke-Checked python -m pip install --upgrade pip
Invoke-Checked python -m pip install ".[heic]" pyinstaller
& "$PSScriptRoot\get-tools.ps1"
Invoke-Checked python -m PyInstaller --noconfirm packaging\windows\rotary.spec

$version = (python -c "import rotary; print(rotary.__version__)").Trim()
$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($iscc) {
    Invoke-Checked $iscc "/DAppVersion=$version" packaging\windows\rotary.iss
    Write-Host "Installer: packaging\windows\Output\Rotary-Setup-$version.exe"
} else {
    Write-Warning "Inno Setup not found, so no installer was built. dist\Rotary\Rotary.exe runs on its own."
}
