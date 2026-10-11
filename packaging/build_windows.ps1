# Build "Reelay.exe" (folder build) and zip it for distribution.
# Run in PowerShell from the project root:  .\packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv\Scripts\python.exe")) { py -3 -m venv .venv }
$py = ".venv\Scripts\python.exe"

& $py -m pip install -q -r requirements.txt -r requirements-dev.txt
& $py packaging\make_icons.py
& $py -m PyInstaller --noconfirm --clean packaging\Reelay.spec

# Optional Authenticode signing (Windows SmartScreen trusts signed apps sooner). Off unless one of these is set:
#   REELAY_WIN_SIGN_THUMBPRINT   SHA-1 thumbprint of a code-signing certificate in the user's store (or a
#                                hardware token / cloud HSM exposed there)
#   REELAY_WIN_PFX + REELAY_WIN_PFX_PASSWORD   a .pfx file (keep it out of the repository)
#   REELAY_WIN_TIMESTAMP_URL     RFC 3161 timestamp server (default: DigiCert's)
$thumb = $env:REELAY_WIN_SIGN_THUMBPRINT
$pfx = $env:REELAY_WIN_PFX
if ($thumb -or $pfx) {
    $signtool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
    if (-not $signtool) {
        $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $signtool) { throw "signtool.exe not found: install the Windows SDK (Signing Tools)." }
    $ts = if ($env:REELAY_WIN_TIMESTAMP_URL) { $env:REELAY_WIN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    $who = if ($thumb) { @("/sha1", $thumb) } else { @("/f", $pfx, "/p", $env:REELAY_WIN_PFX_PASSWORD) }
    # Reelay's own executables and the Python/Qt binaries PyInstaller bundled (already-signed ones are kept).
    $targets = Get-ChildItem "dist\Reelay" -Recurse -Include *.exe, *.dll, *.pyd |
        Where-Object { (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid" }
    foreach ($t in $targets) {
        & $signtool sign /fd SHA256 /tr $ts /td SHA256 @who $t.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "signtool failed on $($t.FullName)" }
    }
    & $signtool verify /pa "dist\Reelay\Reelay.exe"
    if ($LASTEXITCODE -ne 0) { throw "The signature on Reelay.exe didn't verify." }
    Write-Host "Signed $($targets.Count) files."
} else {
    Write-Host "Not signed (set REELAY_WIN_SIGN_THUMBPRINT or REELAY_WIN_PFX to sign)."
}

$version = & $py -c "import reelay; print(reelay.__version__)"
$zip = "dist\Reelay-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\Reelay" -DestinationPath $zip

Write-Host "Built: dist\Reelay\Reelay.exe"
Write-Host "Built: $zip"
