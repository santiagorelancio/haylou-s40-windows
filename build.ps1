# Empaqueta e instala Haylou S40 (solo para el usuario actual, sin admin).
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
if (-not (Test-Path "$here\icon.ico")) { Push-Location $here; py make_icon.py; Pop-Location }
$work = Join-Path $env:TEMP 'haylou_s40_build'
$dist = Join-Path $work 'dist'
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work | Out-Null

py -m PyInstaller --noconfirm --clean --onedir --windowed --name "Haylou S40" `
  --workpath "$work\b" --specpath $work --distpath $dist `
  --icon "$here\icon.ico" --add-data "$here\icon.ico;." `
  --add-data "$here\tcl_stub;_tcl_data" --add-data "$here\tcl_stub;_tk_data" --runtime-hook "$here\rthook_tcl9.py" `
  --paths $here --collect-submodules bleak --collect-all winrt `
  --hidden-import pystray._win32 `
  --exclude-module flet --exclude-module numpy --exclude-module pycaw `
  "$here\haylou_s40.py"
if (-not (Test-Path "$dist\Haylou S40\Haylou S40.exe")) { throw "Fallo el empaquetado" }

$target = Join-Path $env:LOCALAPPDATA 'Programs\HaylouS40'
Get-Process 'Haylou S40' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 1
Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
Copy-Item "$dist\Haylou S40" $target -Recurse
$exe = Join-Path $target 'Haylou S40.exe'

$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$ws = New-Object -ComObject WScript.Shell
$l = $ws.CreateShortcut((Join-Path $lnkDir 'Haylou S40.lnk'))
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
Set-ItemProperty $k DisplayName 'Haylou S40'
Set-ItemProperty $k DisplayIcon $exe
Set-ItemProperty $k Publisher 'Local'
Set-ItemProperty $k InstallLocation $target
Set-ItemProperty $k UninstallString "powershell -NoProfile -ExecutionPolicy Bypass -File `"$un`""
Write-Host "Instalado en $target" -ForegroundColor Green

