# Empaqueta Haylou S40 con PyInstaller y, salvo -NoInstall, lo instala solo para el
# usuario actual (sin admin). La misma receta la usan los workflows de GitHub.
#
#   build.ps1                                  # empaqueta e instala
#   build.ps1 -NoInstall -DistDir dist -Zip    # solo genera dist\Haylou S40 y el .zip (CI)
#
# Firma opcional: si hay certificado (parametros o variables SIGN_CERT_PFX_BASE64 /
# SIGN_CERT_PASSWORD), se firma el .exe con signtool antes de instalar o comprimir.
param(
    [switch]$NoInstall,
    [string]$DistDir,
    [switch]$Zip,
    [string]$Python = 'py',
    [string]$CertPath,
    [string]$CertPassword = $env:SIGN_CERT_PASSWORD
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$app = 'Haylou S40'
if (-not (Test-Path "$here\icon.ico")) {
    Push-Location $here
    & $Python make_icon.py
    if ($LASTEXITCODE) { throw "No se pudo generar icon.ico" }
    Pop-Location
}
$work = Join-Path $env:TEMP 'haylou_s40_build'
if (-not $DistDir) { $DistDir = Join-Path $work 'dist' }
$DistDir = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($DistDir)) { $DistDir } else { Join-Path $here $DistDir }))
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null

& $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name $app `
  --workpath "$work\b" --specpath $work --distpath $DistDir `
  --icon "$here\icon.ico" --add-data "$here\icon.ico;." `
  --add-data "$here\tcl_stub;_tcl_data" --add-data "$here\tcl_stub;_tk_data" --runtime-hook "$here\rthook_tcl9.py" `
  --paths $here --collect-submodules bleak --collect-all winrt `
  --hidden-import pystray._win32 `
  --exclude-module flet --exclude-module numpy --exclude-module pycaw `
  "$here\haylou_s40.py"
$built = Join-Path $DistDir $app
if (-not (Test-Path "$built\$app.exe")) { throw "Fallo el empaquetado" }

# --- firma (opcional) ---
$tmpCert = $null
if (-not $CertPath -and $env:SIGN_CERT_PFX_BASE64) {
    $tmpCert = Join-Path $work 'cert.pfx'
    [IO.File]::WriteAllBytes($tmpCert, [Convert]::FromBase64String($env:SIGN_CERT_PFX_BASE64))
    $CertPath = $tmpCert
}
if ($CertPath) {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) { throw "No se encontro signtool.exe (Windows SDK)" }
    try {
        & $signtool.FullName sign /f $CertPath /p $CertPassword /fd SHA256 `
            /tr http://timestamp.digicert.com /td SHA256 /d $app "$built\$app.exe"
        if ($LASTEXITCODE) { throw "Fallo la firma del ejecutable" }
        Write-Host "Ejecutable firmado" -ForegroundColor Green
    } finally {
        if ($tmpCert) { Remove-Item $tmpCert -Force -ErrorAction SilentlyContinue }
    }
}

if ($Zip) {
    $zipPath = Join-Path (Split-Path $DistDir) 'Haylou-S40-Windows.zip'
    Remove-Item $zipPath -ErrorAction SilentlyContinue
    Compress-Archive -Path $built -DestinationPath $zipPath
    Write-Host "Generado $zipPath" -ForegroundColor Green
}
if ($NoInstall) { return }

# --- instalacion para el usuario actual ---
$target = Join-Path $env:LOCALAPPDATA 'Programs\HaylouS40'
Get-Process $app -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 1
Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
Copy-Item $built $target -Recurse
$exe = Join-Path $target "$app.exe"

$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$ws = New-Object -ComObject WScript.Shell
$l = $ws.CreateShortcut((Join-Path $lnkDir "$app.lnk"))
$l.TargetPath = $exe; $l.WorkingDirectory = $target; $l.IconLocation = "$exe,0"; $l.Save()

# Desinstalador
$un = Join-Path $target 'desinstalar.ps1'
@"
Get-Process 'Haylou S40' -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name HaylouS40 -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HaylouS40' -ErrorAction SilentlyContinue
Remove-Item "`$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Haylou S40.lnk" -ErrorAction SilentlyContinue
Remove-Item "`$env:APPDATA\HaylouS40" -Recurse -ErrorAction SilentlyContinue
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile -Command Start-Sleep 2; Remove-Item -Recurse -Force '$target'"
"@ | Set-Content $un -Encoding UTF8
$k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\HaylouS40'
New-Item $k -Force | Out-Null
Set-ItemProperty $k DisplayName $app
Set-ItemProperty $k DisplayIcon $exe
Set-ItemProperty $k Publisher 'Local'
Set-ItemProperty $k InstallLocation $target
Set-ItemProperty $k UninstallString "powershell -NoProfile -ExecutionPolicy Bypass -File `"$un`""
Write-Host "Instalado en $target" -ForegroundColor Green
