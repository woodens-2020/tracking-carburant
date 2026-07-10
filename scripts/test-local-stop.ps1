<#
Arrete l'environnement de test local (backend 8002 + frontend 3001).
La base station_db_test n'est pas supprimee (reutilisee au prochain lancement,
sauf refresh via test-local-start.ps1 sans -NoRefreshDb).
#>
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "scripts\.test-local.pid.json"

if (Test-Path $pidFile) {
    $pids = Get-Content $pidFile | ConvertFrom-Json
    foreach ($p in @($pids.backend, $pids.frontend)) {
        if ($p) {
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# Filet de securite : au cas ou le PID aurait change (npm relance un process enfant)
$ports = @(8002, 3001)
foreach ($port in $ports) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Environnement de test arrete (ports 8002 et 3001 liberes)." -ForegroundColor Green
