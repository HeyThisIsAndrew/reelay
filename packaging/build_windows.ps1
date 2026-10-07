# Build "Reelay.exe" (folder build) and zip it for distribution.
# Run in PowerShell from the project root:  .\packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
$py = ".venv\Scripts\python.exe"

& $py -m pip install -q -r requirements.txt -r requirements-dev.txt
& $py packaging\make_icons.py
& $py -m PyInstaller --noconfirm --clean packaging\Reelay.spec

$version = & $py -c "import reelay; print(reelay.__version__)"
$zip = "dist\Reelay-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\Reelay" -DestinationPath $zip

Write-Host "Built: dist\Reelay\Reelay.exe"
Write-Host "Built: $zip"
