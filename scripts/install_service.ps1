#Requires -RunAsAdministrator
#Requires -Version 5.1
# install_service.ps1 — Installation du service Windows KonektaApp via NSSM
#
# Ce script :
#   1. Télécharge NSSM (Non-Sucking Service Manager) depuis le site officiel
#   2. Installe le service Windows "KonektaApp"
#   3. Configure le redémarrage automatique en cas de crash (3 tentatives max)
#   4. Redirige les logs stdout/stderr vers C:\Logs\KonektaApp\
#   5. Démarre le service immédiatement et vérifie qu'il répond
#
# Usage : powershell.exe -ExecutionPolicy Bypass -File install_service.ps1

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
$SERVICE_NAME   = "KonektaApp"
$SERVICE_DISPLAY = "Konekta — Bon Prix Application"
$SERVICE_DESC   = "Serveur FastAPI/Uvicorn pour Konekta Bon Prix — port 8001"

$PYTHON_EXE     = "C:\Users\Homilus Woodens\AppData\Local\Python\bin\python.exe"
$APP_DIR        = "C:\Users\Homilus Woodens\OneDrive\Documents\Tracking-Carburant\backend"
$APP_ARGS       = "-m uvicorn main:app --host 0.0.0.0 --port 8001"

$LOG_DIR        = "C:\Logs\KonektaApp"
$NSSM_DIR       = "C:\nssm"
$NSSM_ZIP_URL   = "https://nssm.cc/release/nssm-2.24.zip"
$NSSM_ZIP_PATH  = "$env:TEMP\nssm-2.24.zip"
$NSSM_EXE       = "$NSSM_DIR\nssm-2.24\win64\nssm.exe"
$PORT           = 8001

Write-Host ""
Write-Host "=== Installation du service Windows KonektaApp ==="
Write-Host ""

# ── Vérifications préalables ──────────────────────────────────────────────────
if (-not (Test-Path $PYTHON_EXE)) {
    Write-Host "ERREUR : Python introuvable : $PYTHON_EXE" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $APP_DIR)) {
    Write-Host "ERREUR : Répertoire app introuvable : $APP_DIR" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "$APP_DIR\main.py")) {
    Write-Host "ERREUR : main.py introuvable dans $APP_DIR" -ForegroundColor Red
    exit 1
}

Write-Host "  [OK] Python  : $PYTHON_EXE"
Write-Host "  [OK] App dir : $APP_DIR"

# ── Création du répertoire de logs ────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null
Write-Host "  [OK] Logs    : $LOG_DIR"

# ── Téléchargement et extraction de NSSM ─────────────────────────────────────
if (Test-Path $NSSM_EXE) {
    Write-Host "  [OK] NSSM déjà présent : $NSSM_EXE"
} else {
    Write-Host ""
    Write-Host "Téléchargement de NSSM depuis $NSSM_ZIP_URL ..."
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        # nssm.cc nécessite un User-Agent navigateur (renvoie 503 sinon)
        Invoke-WebRequest -Uri $NSSM_ZIP_URL -OutFile $NSSM_ZIP_PATH -UseBasicParsing `
            -Headers @{"User-Agent" = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        Write-Host "  Téléchargement terminé."
    } catch {
        Write-Host "  ERREUR téléchargement NSSM : $_" -ForegroundColor Red
        Write-Host "  Solution manuelle :"
        Write-Host "    1. Téléchargez nssm-2.24.zip depuis https://nssm.cc/download"
        Write-Host "    2. Extrayez win64\nssm.exe vers $NSSM_DIR\nssm-2.24\win64\"
        Write-Host "    3. Relancez ce script."
        exit 1
    }

    New-Item -ItemType Directory -Force -Path $NSSM_DIR | Out-Null
    Expand-Archive -Path $NSSM_ZIP_PATH -DestinationPath $NSSM_DIR -Force
    Remove-Item $NSSM_ZIP_PATH -Force

    if (-not (Test-Path $NSSM_EXE)) {
        Write-Host "  ERREUR : nssm.exe introuvable après extraction dans $NSSM_EXE" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] NSSM installé : $NSSM_EXE"
}

# ── Arrêt et suppression du service existant si présent ──────────────────────
$existingService = Get-Service -Name $SERVICE_NAME -ErrorAction SilentlyContinue
if ($existingService) {
    Write-Host ""
    Write-Host "  Service existant détecté — suppression..."
    if ($existingService.Status -eq "Running") {
        & $NSSM_EXE stop $SERVICE_NAME | Out-Null
        Start-Sleep -Seconds 3
    }
    & $NSSM_EXE remove $SERVICE_NAME confirm | Out-Null
    Start-Sleep -Seconds 2
    Write-Host "  Service précédent supprimé."
}

# ── Installation du service ───────────────────────────────────────────────────
Write-Host ""
Write-Host "Installation du service $SERVICE_NAME ..."

& $NSSM_EXE install $SERVICE_NAME $PYTHON_EXE $APP_ARGS
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERREUR nssm install : code $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

# ── Configuration du service ──────────────────────────────────────────────────
& $NSSM_EXE set $SERVICE_NAME DisplayName  $SERVICE_DISPLAY
& $NSSM_EXE set $SERVICE_NAME Description  $SERVICE_DESC
& $NSSM_EXE set $SERVICE_NAME AppDirectory $APP_DIR
& $NSSM_EXE set $SERVICE_NAME Start        SERVICE_AUTO_START

# Logs stdout et stderr
& $NSSM_EXE set $SERVICE_NAME AppStdout    "$LOG_DIR\stdout.log"
& $NSSM_EXE set $SERVICE_NAME AppStderr    "$LOG_DIR\stderr.log"
& $NSSM_EXE set $SERVICE_NAME AppStdoutCreationDisposition 4  # append
& $NSSM_EXE set $SERVICE_NAME AppStderrCreationDisposition 4  # append

# Rotation des logs : quotidienne ou à 10 Mo
& $NSSM_EXE set $SERVICE_NAME AppRotateFiles  1
& $NSSM_EXE set $SERVICE_NAME AppRotateOnline 1
& $NSSM_EXE set $SERVICE_NAME AppRotateSeconds 86400
& $NSSM_EXE set $SERVICE_NAME AppRotateBytes  10485760

# Délai de redémarrage en cas de crash : 10 secondes
& $NSSM_EXE set $SERVICE_NAME AppRestartDelay 10000

# Throttle : si le service tourne > 60s avant de crasher, remettre à zéro
# le compteur d'échecs Windows (évite de bloquer sur crash accidentel rare)
& $NSSM_EXE set $SERVICE_NAME AppThrottle 60000

# Arrêt propre : envoyer Ctrl+C avant SIGKILL
& $NSSM_EXE set $SERVICE_NAME AppStopMethodSkip 0

Write-Host "  [OK] Service configuré."

# ── Politique de récupération Windows (3 tentatives max) ─────────────────────
# 1er crash : redémarrage après 10s
# 2e crash  : redémarrage après 30s
# 3e+ crash : aucune action (évite boucle infinie si crash systématique)
# Fenêtre de réinitialisation du compteur d'échecs : 1 heure
sc.exe failure $SERVICE_NAME reset= 3600 actions= restart/10000/restart/30000/run/0 | Out-Null
# Activer la politique de récupération y compris sur crash non-zero
sc.exe failureflag $SERVICE_NAME 1 | Out-Null

Write-Host "  [OK] Politique de récupération : 2 redémarrages max (10s, 30s), puis arrêt."

# ── Démarrage du service ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "Démarrage du service $SERVICE_NAME ..."
Start-Service -Name $SERVICE_NAME
Start-Sleep -Seconds 5

$svc = Get-Service -Name $SERVICE_NAME
if ($svc.Status -eq "Running") {
    Write-Host "  [OK] Service démarré — statut : $($svc.Status)" -ForegroundColor Green
} else {
    Write-Host "  [ERREUR] Service non démarré — statut : $($svc.Status)" -ForegroundColor Red
    Write-Host "  Consultez les logs : $LOG_DIR\stderr.log"
    exit 1
}

# ── Vérification réseau : l'app répond sur le port ───────────────────────────
Write-Host ""
Write-Host "Vérification que l'application répond sur le port $PORT ..."
$maxTries = 12
$tries    = 0
$ok       = $false

while ($tries -lt $maxTries) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$PORT" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $ok = $true
        break
    } catch {
        $tries++
        if ($tries -lt $maxTries) {
            Write-Host "  Tentative $tries/$maxTries — en attente..." -ForegroundColor Yellow
            Start-Sleep -Seconds 5
        }
    }
}

if ($ok) {
    Write-Host "  [OK] Application répond sur http://localhost:$PORT" -ForegroundColor Green
} else {
    Write-Host "  [AVERTISSEMENT] L'application ne répond pas encore sur le port $PORT" -ForegroundColor Yellow
    Write-Host "  Le service est démarré mais l'app peut nécessiter quelques secondes."
    Write-Host "  Vérifiez : http://localhost:$PORT"
    Write-Host "  Logs     : $LOG_DIR\stderr.log"
}

# ── Résumé ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Installation terminée ==="
Write-Host ""
Write-Host "  Service     : $SERVICE_NAME"
Write-Host "  Statut      : $((Get-Service -Name $SERVICE_NAME).Status)"
Write-Host "  Démarrage   : Automatique (au boot du serveur)"
Write-Host "  Récupération: Redémarrage auto x2 (10s, 30s) puis arrêt définitif"
Write-Host "  Logs stdout : $LOG_DIR\stdout.log"
Write-Host "  Logs stderr : $LOG_DIR\stderr.log"
Write-Host ""
Write-Host "  Commandes de gestion :"
Write-Host "    Statut  : Get-Service $SERVICE_NAME"
Write-Host "    Arrêt   : Stop-Service $SERVICE_NAME"
Write-Host "    Démarrage: Start-Service $SERVICE_NAME"
Write-Host "    Relance : Restart-Service $SERVICE_NAME"
Write-Host ""
Write-Host "  Consultez scripts\OPERATIONS.md pour la documentation complète."
Write-Host ""
