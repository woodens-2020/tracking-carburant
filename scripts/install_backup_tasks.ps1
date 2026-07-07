#Requires -RunAsAdministrator
#Requires -Version 5.1
# install_backup_tasks.ps1 — Création des tâches planifiées Windows pour la sauvegarde
#
# Crée deux tâches :
#   - KonektaBackup_02h00  (exécution quotidienne à 02h00)
#   - KonektaBackup_14h00  (exécution quotidienne à 14h00)
#
# Les tâches s'exécutent sous le compte SYSTEM — aucune session utilisateur
# ne doit être ouverte. PostgreSQL doit être démarré au moment de l'exécution.
#
# Usage : powershell.exe -ExecutionPolicy Bypass -File install_backup_tasks.ps1

$ErrorActionPreference = "Stop"

$SCRIPT_DIR   = Split-Path -Parent $MyInvocation.MyCommand.Path
$BACKUP_SCRIPT = "$SCRIPT_DIR\backup_pg.ps1"
$TASK_FOLDER  = "Konekta"

Write-Host ""
Write-Host "=== Installation des tâches planifiées de sauvegarde ==="
Write-Host "    Script : $BACKUP_SCRIPT"
Write-Host ""

# ── Vérification que le script de backup existe ───────────────────────────────
if (-not (Test-Path $BACKUP_SCRIPT)) {
    Write-Host "ERREUR : Script backup introuvable : $BACKUP_SCRIPT" -ForegroundColor Red
    exit 1
}

# ── Définition de l'action commune ───────────────────────────────────────────
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$BACKUP_SCRIPT`""

# ── Principal : compte SYSTEM, droits élevés ─────────────────────────────────
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -RunLevel Highest `
    -LogonType ServiceAccount

# ── Paramètres généraux ───────────────────────────────────────────────────────
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -RunOnlyIfNetworkAvailable:$false `
    -StartWhenAvailable `
    -WakeToRun:$false

# ── Création du dossier de tâches personnalisé ────────────────────────────────
try {
    $schedService = New-Object -ComObject "Schedule.Service"
    $schedService.Connect()
    $rootFolder = $schedService.GetFolder("\")
    try {
        $rootFolder.GetFolder($TASK_FOLDER) | Out-Null
        Write-Host "  Dossier de tâches '\$TASK_FOLDER' déjà présent."
    } catch {
        $rootFolder.CreateFolder($TASK_FOLDER) | Out-Null
        Write-Host "  Dossier de tâches '\$TASK_FOLDER' créé."
    }
} catch {
    Write-Host "  Avertissement : impossible de créer le dossier de tâches. Les tâches seront à la racine." -ForegroundColor Yellow
    $TASK_FOLDER = ""
}

$taskPath = if ($TASK_FOLDER) { "\$TASK_FOLDER\" } else { "\" }

# ── Tâche 1 : 02h00 ───────────────────────────────────────────────────────────
$trigger02h = New-ScheduledTaskTrigger -Daily -At "02:00"
$taskName02 = "KonektaBackup_02h00"

try {
    Unregister-ScheduledTask -TaskName $taskName02 -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName   $taskName02 `
        -TaskPath   $taskPath `
        -Action     $action `
        -Trigger    $trigger02h `
        -Principal  $principal `
        -Settings   $settings `
        -Description "Sauvegarde automatique PostgreSQL station_db — 02h00 quotidien" `
        -Force | Out-Null
    Write-Host "  [OK] Tâche créée : ${taskPath}${taskName02}" -ForegroundColor Green
} catch {
    Write-Host "  [ERREUR] Tâche 02h00 : $_" -ForegroundColor Red
    exit 1
}

# ── Tâche 2 : 14h00 ───────────────────────────────────────────────────────────
$trigger14h = New-ScheduledTaskTrigger -Daily -At "14:00"
$taskName14 = "KonektaBackup_14h00"

try {
    Unregister-ScheduledTask -TaskName $taskName14 -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName   $taskName14 `
        -TaskPath   $taskPath `
        -Action     $action `
        -Trigger    $trigger14h `
        -Principal  $principal `
        -Settings   $settings `
        -Description "Sauvegarde automatique PostgreSQL station_db — 14h00 quotidien" `
        -Force | Out-Null
    Write-Host "  [OK] Tâche créée : ${taskPath}${taskName14}" -ForegroundColor Green
} catch {
    Write-Host "  [ERREUR] Tâche 14h00 : $_" -ForegroundColor Red
    exit 1
}

# ── Test immédiat : exécution de la tâche 02h00 ───────────────────────────────
Write-Host ""
Write-Host "Test : lancement immédiat de la tâche de sauvegarde..."
Start-ScheduledTask -TaskName $taskName02 -TaskPath $taskPath

Write-Host "  La tâche est en cours d'exécution en arrière-plan."
Write-Host "  Attendez 30 secondes puis vérifiez le log :"
Write-Host "  C:\Backups\konekta\logs\backup_log.txt"
Write-Host ""

# ── Résumé ────────────────────────────────────────────────────────────────────
Write-Host "=== Installation terminée ==="
Write-Host ""
Write-Host "  Tâches créées :"
Write-Host "    - ${taskPath}${taskName02}  (quotidien à 02h00)"
Write-Host "    - ${taskPath}${taskName14}  (quotidien à 14h00)"
Write-Host ""
Write-Host "  Pour vérifier l'état des tâches :"
Write-Host "    Get-ScheduledTask -TaskPath '$taskPath' | Select-Object TaskName, State"
Write-Host ""
Write-Host "  Pour désactiver les sauvegardes temporairement :"
Write-Host "    Disable-ScheduledTask -TaskName '$taskName02' -TaskPath '$taskPath'"
Write-Host "    Disable-ScheduledTask -TaskName '$taskName14' -TaskPath '$taskPath'"
Write-Host ""
