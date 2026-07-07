#Requires -RunAsAdministrator
#Requires -Version 5.1
# setup_pgpass.ps1 — Configuration sécurisée du mot de passe PostgreSQL
#
# À exécuter UNE SEULE FOIS, avec droits administrateur, avant la première
# sauvegarde. Le mot de passe est saisi de façon masquée et n'apparaît jamais
# dans les logs, l'historique PowerShell, ni dans aucun fichier du projet.
#
# Usage : powershell.exe -ExecutionPolicy Bypass -File setup_pgpass.ps1

$ErrorActionPreference = "Stop"

$PGPASS_DIR  = "C:\ProgramData\Konekta"
$PGPASS_FILE = "$PGPASS_DIR\.pgpass"

Write-Host ""
Write-Host "=== Configuration du fichier de mot de passe PostgreSQL ==="
Write-Host "    Ce fichier sera stocké dans : $PGPASS_FILE"
Write-Host "    Il ne sera JAMAIS dans git ni dans le projet."
Write-Host ""

# ── Saisie sécurisée (le mot de passe n'est jamais affiché) ──────────────────
$secPwd = Read-Host -AsSecureString "Mot de passe PostgreSQL pour l'utilisateur 'postgres'"
$bstr   = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secPwd)
$pwd    = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

if ([string]::IsNullOrWhiteSpace($pwd)) {
    Write-Host "ERREUR : mot de passe vide. Abandon." -ForegroundColor Red
    exit 1
}

# ── Création du répertoire ────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $PGPASS_DIR | Out-Null

# ── Écriture du fichier pgpass ────────────────────────────────────────────────
# Format : hostname:port:database:username:password
# Wildcard '*' sur la base autorise pg_dump sur station_db et pg_restore sur
# la base de test (station_suivi_test) sans second fichier.
$content = "localhost:5432:*:postgres:$pwd"
Set-Content -Path $PGPASS_FILE -Value $content -Encoding UTF8 -NoNewline

# ── Sécurisation des permissions ──────────────────────────────────────────────
# Bloquer l'héritage et donner accès explicite uniquement à SYSTEM + Administrateurs
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)

$sysRule   = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "SYSTEM", "Read,Synchronize", "Allow")
$adminRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "Administrators", "FullControl", "Allow")

$acl.AddAccessRule($sysRule)
$acl.AddAccessRule($adminRule)
Set-Acl -Path $PGPASS_FILE -AclObject $acl

# ── Effacement de la variable en mémoire ─────────────────────────────────────
$pwd = $null
[System.GC]::Collect()

# ── Vérification rapide : connexion à PostgreSQL ──────────────────────────────
Write-Host ""
Write-Host "Vérification de la connexion PostgreSQL..."
$env:PGPASSFILE = $PGPASS_FILE
$pgBin = "C:\Program Files\PostgreSQL\18\bin"

try {
    $result = & "$pgBin\psql.exe" -h localhost -p 5432 -U postgres -d station_db -c "SELECT 1 AS ok;" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Connexion OK — le mot de passe est correct." -ForegroundColor Green
    } else {
        Write-Host "  ATTENTION : connexion échouée. Vérifiez le mot de passe." -ForegroundColor Yellow
        Write-Host "  Sortie psql : $result"
        Write-Host "  Relancez ce script pour corriger le fichier."
    }
} catch {
    Write-Host "  ATTENTION : psql.exe introuvable ou erreur : $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Fichier créé  : $PGPASS_FILE" -ForegroundColor Green
Write-Host "  Permissions   : SYSTEM (lecture), Administrateurs (contrôle total)"
Write-Host ""
Write-Host "  Ce fichier est hors du répertoire git."
Write-Host "  Ne le déplacer jamais dans le projet."
Write-Host ""
