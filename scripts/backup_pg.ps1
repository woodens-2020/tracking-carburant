#Requires -Version 5.1
# backup_pg.ps1 — Sauvegarde automatique PostgreSQL
# Planifié : 02h00 et 14h00 via Windows Task Scheduler
# Exécution : powershell.exe -NonInteractive -ExecutionPolicy Bypass -File backup_pg.ps1

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
$PG_BIN         = "C:\Program Files\PostgreSQL\18\bin"
$DB_HOST        = "localhost"
$DB_PORT        = "5432"
$DB_NAME        = "station_db"
$DB_USER        = "postgres"
$BACKUP_DIR     = "C:\Backups\konekta"
$PGPASSFILE     = "C:\ProgramData\Konekta\.pgpass"
$LOG_FILE       = "C:\Backups\konekta\logs\backup_log.txt"
$RETENTION_DAYS = 14

# ── Initialisation des répertoires ────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $BACKUP_DIR             | Out-Null
New-Item -ItemType Directory -Force -Path "$BACKUP_DIR\logs"      | Out-Null

# ── Horodatage ────────────────────────────────────────────────────────────────
$now        = Get-Date
$timestamp  = $now.ToString("yyyy-MM-dd_HHhmm")
$dateLabel  = $now.ToString("yyyy-MM-dd HH:mm:ss")
$dumpFile   = "$BACKUP_DIR\station_suivi_backup_$timestamp.dump"

function Write-Log {
    param([string]$msg, [string]$level = "INFO")
    $line = "[$dateLabel] [$level] $msg"
    try {
        Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
    } catch {
        # Si le fichier log est verrouillé, ne pas planter la sauvegarde
    }
    Write-Host $line
}

Write-Log "================================================================"
Write-Log "DÉBUT SAUVEGARDE — Base: $DB_NAME"

# ── Vérification du fichier pgpass ────────────────────────────────────────────
if (-not (Test-Path $PGPASSFILE)) {
    Write-Log "ERREUR : Fichier pgpass introuvable : $PGPASSFILE" "ERROR"
    Write-Log "         Exécutez d'abord : scripts\setup_pgpass.ps1" "ERROR"
    exit 1
}
$env:PGPASSFILE = $PGPASSFILE

# ── Vérification que pg_dump existe ──────────────────────────────────────────
$pgDump = "$PG_BIN\pg_dump.exe"
if (-not (Test-Path $pgDump)) {
    Write-Log "ERREUR : pg_dump introuvable : $pgDump" "ERROR"
    exit 1
}

# ── Exécution pg_dump ─────────────────────────────────────────────────────────
$startTime = Get-Date

try {
    & $pgDump `
        "--host=$DB_HOST" `
        "--port=$DB_PORT" `
        "--username=$DB_USER" `
        "--format=custom" `
        "--compress=9" `
        "--file=$dumpFile" `
        $DB_NAME 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump a terminé avec le code d'erreur $LASTEXITCODE"
    }

    if (-not (Test-Path $dumpFile)) {
        throw "Le fichier dump n'a pas été créé : $dumpFile"
    }

    $durationSec = [math]::Round(((Get-Date) - $startTime).TotalSeconds, 1)
    $sizeMB      = [math]::Round((Get-Item $dumpFile).Length / 1MB, 2)
    $fileName    = Split-Path $dumpFile -Leaf

    Write-Log "SUCCÈS — Fichier : $fileName | Taille : ${sizeMB} Mo | Durée : ${durationSec}s"

} catch {
    Write-Log "ÉCHEC pg_dump : $_" "ERROR"
    # Supprimer le fichier corrompu s'il existe
    if (Test-Path $dumpFile) { Remove-Item $dumpFile -Force }
    exit 1
}

# ── Rétention : supprime les dumps de plus de $RETENTION_DAYS jours ───────────
$cutoff  = (Get-Date).AddDays(-$RETENTION_DAYS)
$deleted = 0

Get-ChildItem -Path $BACKUP_DIR -Filter "station_suivi_backup_*.dump" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Remove-Item $_.FullName -Force
        $deleted++
        Write-Log "Rétention : supprimé $($_.Name) (plus de $RETENTION_DAYS jours)"
    }

if ($deleted -gt 0) {
    Write-Log "Rétention : $deleted ancien(s) dump(s) supprimé(s)"
} else {
    Write-Log "Rétention : aucun fichier à supprimer"
}

# ── Inventaire final ──────────────────────────────────────────────────────────
$allDumps = Get-ChildItem -Path $BACKUP_DIR -Filter "station_suivi_backup_*.dump"
Write-Log "Dumps conservés : $($allDumps.Count) fichier(s)"
Write-Log "FIN SAUVEGARDE — OK"
Write-Log "================================================================"
exit 0
