#Requires -RunAsAdministrator
#Requires -Version 5.1
# install_iis.ps1 — Configuration IIS pour Konekta Bon Prix
#
# Ce script :
#   1. Active IIS et les fonctionnalités nécessaires
#   2. Télécharge et installe ARR (Application Request Routing)
#   3. Télécharge et installe URL Rewrite Module
#   4. Active le mode proxy dans ARR
#   5. Crée le site IIS "Konekta" sur le port 80
#   6. Copie web.config dans le dossier du site
#
# Usage : powershell.exe -ExecutionPolicy Bypass -File scripts\install_iis.ps1

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
$SITE_NAME    = "Konekta"
$SITE_PORT    = 80
$APP_DIR      = "C:\Users\Homilus Woodens\OneDrive\Documents\Tracking-Carburant"
$SITE_ROOT    = "C:\inetpub\konekta"
$BACKEND_PORT = 8001

$ARR_URL      = "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi"
$REWRITE_URL  = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi"

Write-Host ""
Write-Host "=== Configuration IIS pour Konekta Bon Prix ===" -ForegroundColor Cyan
Write-Host ""

# ── Étape 1 : Activer IIS ────────────────────────────────────────────────────
Write-Host "1. Activation des fonctionnalités IIS..."
$features = @(
    "IIS-WebServerRole",
    "IIS-WebServer",
    "IIS-CommonHttpFeatures",
    "IIS-DefaultDocument",
    "IIS-StaticContent",
    "IIS-HttpErrors",
    "IIS-HttpRedirect",
    "IIS-ApplicationDevelopment",
    "IIS-NetFxExtensibility45",
    "IIS-ISAPIExtensions",
    "IIS-ISAPIFilter",
    "IIS-HealthAndDiagnostics",
    "IIS-HttpLogging",
    "IIS-Security",
    "IIS-RequestFiltering",
    "IIS-Performance",
    "IIS-WebServerManagementTools",
    "IIS-ManagementConsole",
    "IIS-Metabase"
)
foreach ($f in $features) {
    try {
        Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart -ErrorAction SilentlyContinue | Out-Null
    } catch {}
}
Write-Host "  [OK] IIS activé." -ForegroundColor Green

# ── Étape 2 : Télécharger et installer ARR ───────────────────────────────────
Write-Host ""
Write-Host "2. Installation de Application Request Routing (ARR)..."
$arrMsi = "$env:TEMP\arr_amd64.msi"
if (-not (Test-Path $arrMsi)) {
    Write-Host "   Téléchargement ARR..."
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $ARR_URL -OutFile $arrMsi -UseBasicParsing `
            -Headers @{"User-Agent"="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    } catch {
        Write-Host "   ERREUR téléchargement ARR : $_" -ForegroundColor Red
        Write-Host "   Téléchargez manuellement depuis : $ARR_URL" -ForegroundColor Yellow
        Write-Host "   Puis relancez ce script." -ForegroundColor Yellow
        exit 1
    }
}
Start-Process msiexec.exe -ArgumentList "/i `"$arrMsi`" /quiet /norestart" -Wait
Write-Host "  [OK] ARR installé." -ForegroundColor Green

# ── Étape 3 : Télécharger et installer URL Rewrite ───────────────────────────
Write-Host ""
Write-Host "3. Installation de URL Rewrite Module..."
$rewriteMsi = "$env:TEMP\rewrite_amd64.msi"
if (-not (Test-Path $rewriteMsi)) {
    Write-Host "   Téléchargement URL Rewrite..."
    try {
        Invoke-WebRequest -Uri $REWRITE_URL -OutFile $rewriteMsi -UseBasicParsing `
            -Headers @{"User-Agent"="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    } catch {
        Write-Host "   ERREUR téléchargement URL Rewrite : $_" -ForegroundColor Red
        Write-Host "   Téléchargez manuellement depuis : $REWRITE_URL" -ForegroundColor Yellow
        exit 1
    }
}
Start-Process msiexec.exe -ArgumentList "/i `"$rewriteMsi`" /quiet /norestart" -Wait
Write-Host "  [OK] URL Rewrite installé." -ForegroundColor Green

# ── Étape 4 : Activer le mode proxy ARR ──────────────────────────────────────
Write-Host ""
Write-Host "4. Activation du mode proxy ARR..."
Import-Module WebAdministration -ErrorAction SilentlyContinue
try {
    Set-WebConfigurationProperty -pspath "MACHINE/WEBROOT/APPHOST" `
        -filter "system.webServer/proxy" -name "enabled" -value "True"
    Write-Host "  [OK] Mode proxy activé." -ForegroundColor Green
} catch {
    Write-Host "  [AVERTISSEMENT] Impossible d'activer le proxy ARR via PowerShell." -ForegroundColor Yellow
    Write-Host "  Activez-le manuellement dans IIS Manager > Server > Application Request Routing Cache > Enable Proxy." -ForegroundColor Yellow
}

# ── Étape 5 : Créer le dossier racine du site IIS ────────────────────────────
Write-Host ""
Write-Host "5. Création du dossier site IIS..."
New-Item -ItemType Directory -Force -Path $SITE_ROOT | Out-Null

# Copier web.config dans le dossier du site
$webConfig = Join-Path $APP_DIR "web.config"
if (Test-Path $webConfig) {
    Copy-Item $webConfig $SITE_ROOT -Force
    Write-Host "  [OK] web.config copié dans $SITE_ROOT" -ForegroundColor Green
} else {
    Write-Host "  [ERREUR] web.config introuvable dans $APP_DIR" -ForegroundColor Red
    Write-Host "  Copiez-le manuellement depuis le dossier du projet." -ForegroundColor Yellow
}

# ── Étape 6 : Créer le site IIS ──────────────────────────────────────────────
Write-Host ""
Write-Host "6. Configuration du site IIS '$SITE_NAME'..."

Import-Module WebAdministration

# Supprimer le site Default Web Site sur port 80 s'il existe
$defaultSite = Get-Website -Name "Default Web Site" -ErrorAction SilentlyContinue
if ($defaultSite -and $defaultSite.Bindings.Collection.bindingInformation -like "*:80:*") {
    Write-Host "   Arrêt du Default Web Site (libération du port 80)..."
    Stop-Website -Name "Default Web Site" -ErrorAction SilentlyContinue
}

# Créer ou reconfigurer le site Konekta
$existingSite = Get-Website -Name $SITE_NAME -ErrorAction SilentlyContinue
if ($existingSite) {
    Write-Host "   Site '$SITE_NAME' existant — mise à jour..."
    Set-ItemProperty "IIS:\Sites\$SITE_NAME" -Name physicalPath -Value $SITE_ROOT
} else {
    New-Website -Name $SITE_NAME -Port $SITE_PORT -PhysicalPath $SITE_ROOT -Force | Out-Null
}

Start-Website -Name $SITE_NAME -ErrorAction SilentlyContinue
Write-Host "  [OK] Site IIS '$SITE_NAME' configuré sur le port $SITE_PORT." -ForegroundColor Green

# ── Vérification finale ───────────────────────────────────────────────────────
Write-Host ""
Write-Host "7. Vérification..."

$site = Get-Website -Name $SITE_NAME -ErrorAction SilentlyContinue
if ($site -and $site.State -eq "Started") {
    Write-Host "  [OK] Site IIS démarré." -ForegroundColor Green
} else {
    Write-Host "  [AVERTISSEMENT] Vérifiez l'état du site dans IIS Manager." -ForegroundColor Yellow
}

# Tester la connectivité vers le backend
try {
    $r = Invoke-WebRequest -Uri "http://localhost:$BACKEND_PORT" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    Write-Host "  [OK] Backend FastAPI répond sur le port $BACKEND_PORT." -ForegroundColor Green
} catch {
    Write-Host "  [AVERTISSEMENT] Le backend FastAPI ne répond pas sur le port $BACKEND_PORT." -ForegroundColor Yellow
    Write-Host "  Assurez-vous que le service KonektaApp est démarré : Start-Service KonektaApp" -ForegroundColor Yellow
}

# ── Résumé ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Installation IIS terminée ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Site IIS    : $SITE_NAME"
Write-Host "  Port        : $SITE_PORT (HTTP)"
Write-Host "  Dossier     : $SITE_ROOT"
Write-Host "  Proxy vers  : http://localhost:$BACKEND_PORT"
Write-Host ""
Write-Host "  Flux de requêtes :"
Write-Host "  Navigateur → IIS :80 → proxy ARR → FastAPI :$BACKEND_PORT"
Write-Host ""
Write-Host "  Prochaines étapes :"
Write-Host "  1. Vérifiez que KonektaApp tourne : Get-Service KonektaApp"
Write-Host "  2. Testez depuis un autre PC : http://$(hostname)"
Write-Host "  3. Pour HTTPS : configurez un certificat SSL dans IIS Manager"
Write-Host ""
Write-Host "  Consultez scripts\OPERATIONS.md pour la documentation complète."
Write-Host ""
