#Requires -Version 5.1
# exporter_donnees.ps1 — Export complet des données pour migration
#
# Exporte toutes les données en fichier SQL (INSERT statements).
# Le fichier produit peut être donné à Claude pour l'importer sur
# un nouveau serveur après avoir fait les migrations Alembic.
#
# Usage : powershell.exe -ExecutionPolicy Bypass -File scripts\exporter_donnees.ps1

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
$PG_BIN      = "C:\Program Files\PostgreSQL\18\bin"
$PGPASS_FILE = "C:\ProgramData\Konekta\.pgpass"
$DB_NAME     = "station_db"
$DB_HOST     = "localhost"
$DB_PORT     = "5432"
$DB_USER     = "postgres"

$DATE        = Get-Date -Format "yyyy-MM-dd_HHhmm"
$OUT_DIR     = "C:\Backups\konekta"
$OUT_SQL     = "$OUT_DIR\donnees_migration_$DATE.sql"
$OUT_CSV_DIR = "$OUT_DIR\csv_migration_$DATE"
$PSQL        = "$PG_BIN\psql.exe"
$PGDUMP      = "$PG_BIN\pg_dump.exe"

Write-Host ""
Write-Host "=== Export des données Konekta pour migration ===" -ForegroundColor Cyan
Write-Host ""

# ── Vérifications ─────────────────────────────────────────────────────────────
if (-not (Test-Path $PGDUMP)) {
    Write-Host "ERREUR : pg_dump introuvable dans $PG_BIN" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $PGPASS_FILE)) {
    Write-Host "ERREUR : Fichier pgpass introuvable : $PGPASS_FILE" -ForegroundColor Red
    Write-Host "Exécutez d'abord : scripts\setup_pgpass.ps1" -ForegroundColor Yellow
    exit 1
}

New-Item -ItemType Directory -Force -Path $OUT_DIR     | Out-Null
New-Item -ItemType Directory -Force -Path $OUT_CSV_DIR | Out-Null

$env:PGPASSFILE = $PGPASS_FILE

# ── Export SQL (INSERT statements — lisible par Claude) ───────────────────────
Write-Host "Export SQL en cours..."
Write-Host "  Fichier : $OUT_SQL"
Write-Host ""

# En-tête du fichier SQL
@"
-- ============================================================
-- EXPORT DONNÉES KONEKTA BON PRIX
-- Date       : $DATE
-- Base source: $DB_NAME @ $DB_HOST:$DB_PORT
-- ============================================================
--
-- INSTRUCTIONS POUR CLAUDE :
-- 1. Sur le nouveau serveur, créer la base de données :
--    createdb -U postgres station_db
-- 2. Appliquer les migrations Alembic (crée les tables) :
--    cd backend && alembic upgrade head
-- 3. Importer ce fichier :
--    psql -U postgres -d station_db -f ce_fichier.sql
-- 4. Vérifier les comptages en bas de ce fichier.
--
-- ============================================================

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

"@ | Out-File -FilePath $OUT_SQL -Encoding UTF8

# Export données table par table (ordre respectant les FK)
$TABLES = @(
    "roles",
    "employes",
    "utilisateurs",
    "admin_codes",
    "alembic_version",
    "bar_categories",
    "bar_produits",
    "bar_prix_historique",
    "pompes",
    "fiches_journalieres",
    "bar_achats",
    "bar_achats_depenses",
    "bar_ventes",
    "bar_lignes_vente",
    "bar_mouvements_stock",
    "bar_credits",
    "bar_remboursements",
    "bar_commandes",
    "bar_lignes_commande",
    "bar_paiements_employes",
    "bar_sessions_caisse",
    "hotel_chambres",
    "hotel_reservations",
    "hotel_lignes_reservation",
    "cuisine_categories",
    "cuisine_plats",
    "cuisine_ventes",
    "cuisine_lignes_vente",
    "zelle_transactions",
    "audit_logs"
)

$totalLignes = 0

foreach ($table in $TABLES) {
    Write-Host "  → $table" -NoNewline

    # Compter les lignes
    $count = & $PSQL -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -t -c "SELECT COUNT(*) FROM $table;" 2>$null
    $count = ($count -replace '\s','')
    if (-not $count) { $count = "0" }

    Write-Host " ($count lignes)"
    $totalLignes += [int]$count

    # Ajouter commentaire de section dans le SQL
    "`n-- ── Table : $table ($count lignes) ──────────────────────" | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append

    # Export INSERT statements pour cette table
    $tableData = & $PGDUMP `
        -h $DB_HOST -p $DB_PORT -U $DB_USER `
        -d $DB_NAME `
        --data-only `
        --inserts `
        --column-inserts `
        --no-privileges `
        --no-owner `
        --table=$table `
        2>$null

    if ($tableData) {
        # Filtrer pour ne garder que les INSERT (enlever les commentaires pg_dump)
        $insertLines = $tableData | Where-Object { $_ -match "^INSERT INTO" -or $_ -match "^SELECT setval" }
        if ($insertLines) {
            $insertLines | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append
        } else {
            "-- (aucune donnée)" | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append
        }
    } else {
        "-- (table inexistante ou vide)" | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append
    }
}

# Résumé de vérification à la fin du fichier SQL
@"

-- ============================================================
-- VÉRIFICATION APRÈS IMPORT
-- Exécutez ces requêtes pour confirmer que les données sont là :
-- ============================================================
"@ | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append

foreach ($table in $TABLES) {
    $count = & $PSQL -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -t -c "SELECT COUNT(*) FROM $table;" 2>$null
    $count = ($count -replace '\s','')
    if (-not $count) { $count = "0" }
    "-- SELECT '$table', COUNT(*) FROM $table;  -- attendu : $count" | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append
}

"-- ============================================================" | Out-File -FilePath $OUT_SQL -Encoding UTF8 -Append

# ── Export CSV (une feuille par table — lisible dans Excel) ──────────────────
Write-Host ""
Write-Host "Export CSV en cours (pour vérification dans Excel)..."

foreach ($table in $TABLES) {
    $csvFile = "$OUT_CSV_DIR\$table.csv"
    $query = "\COPY $table TO '$csvFile' CSV HEADER ENCODING 'UTF8';"
    & $PSQL -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -c $query 2>$null | Out-Null
}

Write-Host "  [OK] CSV exportés dans : $OUT_CSV_DIR" -ForegroundColor Green

# ── Résumé ────────────────────────────────────────────────────────────────────
$sqlSize = (Get-Item $OUT_SQL).Length / 1KB

Write-Host ""
Write-Host "=== Export terminé ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Fichier SQL    : $OUT_SQL"
Write-Host "  Taille SQL     : $([math]::Round($sqlSize,1)) Ko"
Write-Host "  Total lignes   : $totalLignes"
Write-Host "  Dossier CSV    : $OUT_CSV_DIR"
Write-Host ""
Write-Host "  ┌─────────────────────────────────────────────────────────┐"
Write-Host "  │  COMMENT UTILISER CE FICHIER                            │"
Write-Host "  │                                                         │"
Write-Host "  │  1. Copiez le fichier .sql sur le nouveau serveur       │"
Write-Host "  │  2. Ouvrez une session Claude sur le nouveau serveur    │"
Write-Host "  │  3. Donnez-lui le fichier .sql et dites :               │"
Write-Host "  │                                                         │"
Write-Host "  │   'Importe ces données dans la base station_db.         │"
Write-Host "  │    Les tables existent déjà (Alembic a été lancé).      │"
Write-Host "  │    Utilise psql pour exécuter ce fichier SQL.'          │"
Write-Host "  │                                                         │"
Write-Host "  └─────────────────────────────────────────────────────────┘"
Write-Host ""
