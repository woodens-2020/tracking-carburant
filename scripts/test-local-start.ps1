<#
Lance un environnement de test local isolé de la prod :
  - Backend FastAPI sur http://localhost:8002 (base station_db_test)
  - Frontend Next.js sur http://localhost:3001 (build de prod, proxy vers 8002)
La base station_db_test est recreee a partir d'une copie de station_db a chaque lancement,
sauf si -NoRefreshDb est passe.

Usage:
  .\scripts\test-local-start.ps1
  .\scripts\test-local-start.ps1 -NoRefreshDb
#>
param(
    [switch]$NoRefreshDb
)

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$pgBin = "C:\Program Files\PostgreSQL\18\bin"
$pidFile = Join-Path $root "scripts\.test-local.pid.json"

if (-not $NoRefreshDb) {
    Write-Host "[1/4] Rafraichissement de station_db_test depuis station_db..." -ForegroundColor Cyan
    $env:PGPASSWORD = "admin"
    & "$pgBin\dropdb.exe" -U postgres -h localhost --if-exists station_db_test 2>$null
    & "$pgBin\createdb.exe" -U postgres -h localhost station_db_test
    $dumpFile = Join-Path $env:TEMP "konekta_station_db_refresh.dump"
    & "$pgBin\pg_dump.exe" -U postgres -h localhost -d station_db -F c -f $dumpFile
    & "$pgBin\pg_restore.exe" -U postgres -h localhost -d station_db_test $dumpFile
    Remove-Item $dumpFile -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "[1/4] Rafraichissement de la base ignore (-NoRefreshDb)" -ForegroundColor Yellow
}

Write-Host "[2/4] Demarrage du backend de test (port 8002)..." -ForegroundColor Cyan
$py = Join-Path $backendDir "venv\Scripts\python.exe"
$beOut = Join-Path $root "scripts\.test-backend.out.log"
$beErr = Join-Path $root "scripts\.test-backend.err.log"
$env:ENV_FILE = ".env.test"
$beProc = Start-Process -FilePath $py -ArgumentList "-m","uvicorn","main:app","--host","127.0.0.1","--port","8002" `
    -WorkingDirectory $backendDir -RedirectStandardOutput $beOut -RedirectStandardError $beErr `
    -WindowStyle Hidden -PassThru
Remove-Item Env:\ENV_FILE

Start-Sleep -Seconds 3

Write-Host "[3/4] Build + demarrage du frontend de test (port 3001)..." -ForegroundColor Cyan
$node = "C:\Program Files\nodejs\node.exe"
$npmCli = "C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js"
$env:BACKEND_URL = "http://localhost:8002"
& $node $npmCli run build --prefix $frontendDir 2>&1 | Select-Object -Last 15

$feOut = Join-Path $root "scripts\.test-frontend.out.log"
$feErr = Join-Path $root "scripts\.test-frontend.err.log"
$feProc = Start-Process -FilePath $node -ArgumentList "`"$npmCli`"","run","start","--","-p","3001" `
    -WorkingDirectory $frontendDir -RedirectStandardOutput $feOut -RedirectStandardError $feErr `
    -WindowStyle Hidden -PassThru
Remove-Item Env:\BACKEND_URL

Start-Sleep -Seconds 3

@{ backend = $beProc.Id; frontend = $feProc.Id } | ConvertTo-Json | Set-Content -Path $pidFile -Encoding utf8

Write-Host "[4/4] Pret." -ForegroundColor Green
Write-Host ""
Write-Host "  Backend test : http://localhost:8002  (docs: /docs)"
Write-Host "  Frontend test: http://localhost:3001"
Write-Host "  Base de test : station_db_test (copie isolee de station_db)"
Write-Host ""
Write-Host "  Arreter avec: .\scripts\test-local-stop.ps1"
