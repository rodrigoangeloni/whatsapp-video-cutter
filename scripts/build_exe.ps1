$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $ProjectRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $VenvPython)) {
    throw "No se encontro .venv. Ejecuta primero: python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt"
}
if (-not (Test-Path $PyInstaller)) {
    throw "PyInstaller no esta instalado en .venv. Ejecuta: .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt"
}

Write-Host "[1/2] Generando icono personalizado..."
& $VenvPython (Join-Path $ProjectRoot "scripts\generate_icon.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/2] Compilando WhatsAppVideoCutter.exe con PyInstaller..."
& $PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name WhatsAppVideoCutter `
    --icon "assets\app_icon.ico" `
    --version-file "version_info.txt" `
    --add-data "assets\app_icon.ico;assets" `
    "whatsapp_cutter.py"

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$ExePath = Join-Path $ProjectRoot "dist\WhatsAppVideoCutter.exe"
Write-Host "Compilacion completada: $ExePath"
(Get-Item $ExePath).VersionInfo | Select-Object ProductName, ProductVersion, FileVersion, FileDescription
