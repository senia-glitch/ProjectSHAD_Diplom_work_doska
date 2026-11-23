# Запуск доски для проверки в локальной сети (Windows PowerShell 5.1+).
# Перед первым запуском: установка по README и `docker compose up -d db --wait`.
param([int]$Port = 8090)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Нет окружения .venv: выполните шаги установки из README.' }

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress

Write-Host ''
Write-Host 'Доска запускается. Откройте на любом устройстве этой сети:' -ForegroundColor Cyan
foreach ($ip in $ips) { Write-Host ("  http://{0}:{1}" -f $ip, $Port) }
Write-Host ("На этом компьютере: http://localhost:{0}" -f $Port)
Write-Host 'Если другие устройства не открывают страницу, разрешите порт в брандмауэре (README).'
Write-Host 'Остановка: Ctrl+C'
Write-Host ''

$env:FAT_HOST = '0.0.0.0'
$env:FAT_PORT = "$Port"
$env:FAT_WORKERS = '1'
$env:FAT_RELOAD = 'false'
$env:FAT_LOG_LEVEL = 'info'
& $python -m app.main
