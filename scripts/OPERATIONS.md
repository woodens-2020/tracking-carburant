# Guide opérationnel — Konekta Bon Prix

Ce document couvre les deux piliers de fiabilité opérationnelle :
1. Sauvegardes automatiques PostgreSQL
2. Gestion du service Windows KonektaApp

---

## 1. Procédure de première installation

Exécuter dans cet ordre, chaque script en tant qu'**Administrateur** :

```
# Étape 1 — Configurer le mot de passe PostgreSQL (une seule fois)
powershell -ExecutionPolicy Bypass -File scripts\setup_pgpass.ps1

# Étape 2 — Installer les tâches de sauvegarde planifiées
powershell -ExecutionPolicy Bypass -File scripts\install_backup_tasks.ps1

# Étape 3 — Installer le service Windows KonektaApp
powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
```

---

## 2. Sauvegarde PostgreSQL

### Emplacements

| Fichier | Chemin |
|---------|--------|
| Dumps de sauvegarde | `C:\Backups\konekta\station_suivi_backup_*.dump` |
| Log de sauvegarde | `C:\Backups\konekta\logs\backup_log.txt` |
| Fichier pgpass | `C:\ProgramData\Konekta\.pgpass` |

### Politique de rétention

- **14 jours glissants** : les dumps de plus de 14 jours sont supprimés automatiquement à chaque exécution.
- Le log de sauvegarde est conservé indéfiniment (fichier texte de quelques Ko par jour).
- Chaque dump compressé occupe en général 1–10 Mo selon le volume de données.

### Vérifier qu'une sauvegarde s'est bien passée

```powershell
# Dernières lignes du log
Get-Content C:\Backups\konekta\logs\backup_log.txt -Tail 20

# Lister les dumps existants avec leur taille
Get-ChildItem C:\Backups\konekta\*.dump | Select-Object Name, LastWriteTime,
    @{N="Taille Mo"; E={[math]::Round($_.Length/1MB,2)}} | Sort-Object LastWriteTime
```

### Lancer une sauvegarde manuellement

```powershell
# En tant qu'Administrateur
powershell -ExecutionPolicy Bypass -File "C:\Users\Homilus Woodens\OneDrive\Documents\Tracking-Carburant\scripts\backup_pg.ps1"
```

---

## 3. Restauration d'une sauvegarde

**Lire ceci en entier avant d'agir.** Une restauration écrase les données en place.

### 3a. Restauration sur une base de TEST (recommandée pour vérifier l'intégrité)

```powershell
# Créer la base de test
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -c "CREATE DATABASE station_suivi_test;"

# Restaurer le dump dans la base de test
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" `
    --host=localhost `
    --port=5432 `
    --username=postgres `
    --dbname=station_suivi_test `
    --no-owner `
    --no-privileges `
    --verbose `
    "C:\Backups\konekta\station_suivi_backup_2026-07-07_02h00.dump"

# Vérifier quelques tables
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d station_suivi_test `
    -c "SELECT COUNT(*) FROM users; SELECT COUNT(*) FROM relevés;"

# Nettoyer la base de test quand terminé
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -c "DROP DATABASE station_suivi_test;"
```

### 3b. Restauration en production (données de production effacées)

> **ATTENTION** : Cette opération est irréversible. La base `station_db` actuelle sera
> entièrement remplacée. Effectuez d'abord une sauvegarde manuelle de l'état actuel.

```powershell
# 1. Arrêter l'application
Stop-Service KonektaApp

# 2. Sauvegarder l'état actuel avant restauration
$now = Get-Date -Format "yyyy-MM-dd_HHhmm"
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" `
    --username=postgres --format=custom --compress=9 `
    --file="C:\Backups\konekta\AVANT_RESTAURATION_$now.dump" station_db

# 3. Supprimer et recréer la base
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres `
    -c "DROP DATABASE IF EXISTS station_db; CREATE DATABASE station_db;"

# 4. Restaurer le dump choisi (remplacer le nom du fichier)
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" `
    --host=localhost --port=5432 --username=postgres `
    --dbname=station_db --no-owner --no-privileges `
    "C:\Backups\konekta\station_suivi_backup_2026-07-07_02h00.dump"

# 5. Redémarrer l'application
Start-Service KonektaApp

# 6. Vérifier que l'application répond
Invoke-WebRequest http://localhost:8001 -UseBasicParsing
```

### Résultat attendu de pg_restore

Test effectué le 2026-07-07 avec le dump `station_suivi_backup_2026-07-07_03h41.dump` (197 Ko).
Exit code : **0** — zéro erreur.

Vérification de l'intégrité des données (originale vs restaurée) :

| Table | Lignes originales | Lignes restaurées |
|-------|:-----------------:|:-----------------:|
| utilisateurs | 6 | 6 |
| roles | 13 | 13 |
| pompes | 4 | 4 |
| zelle_transactions | 1 | 1 |
| bar_ventes | 12 | 12 |
| hotel_reservations | 1 | 1 |
| audit_logs | 143 | 143 |
| admin_codes | 3 | 3 |
| alembic_version | 1 | 1 |

**Toutes les tables correspondent exactement.** Exit code 0 = restauration propre.

---

## 4. Gestion du service KonektaApp

### Emplacements

| Fichier | Chemin |
|---------|--------|
| Logs application stdout | `C:\Logs\KonektaApp\stdout.log` |
| Logs application stderr | `C:\Logs\KonektaApp\stderr.log` |
| Binaire NSSM | `C:\nssm\nssm-2.24\win64\nssm.exe` |

### Commandes quotidiennes

```powershell
# Statut du service
Get-Service KonektaApp

# Démarrer
Start-Service KonektaApp

# Arrêter
Stop-Service KonektaApp

# Redémarrer
Restart-Service KonektaApp

# Logs en temps réel (stderr — erreurs Python/uvicorn)
Get-Content C:\Logs\KonektaApp\stderr.log -Tail 50 -Wait

# Logs stdout (requêtes HTTP)
Get-Content C:\Logs\KonektaApp\stdout.log -Tail 50 -Wait
```

### Comportement en cas de crash

Le service est configuré avec la politique suivante :

| Événement | Action |
|-----------|--------|
| 1er crash | Redémarrage automatique après **10 secondes** |
| 2e crash dans la même heure | Redémarrage automatique après **30 secondes** |
| 3e crash et suivants | **Arrêt définitif** — intervention manuelle requise |
| Pas de crash pendant 1 heure | Remise à zéro du compteur d'échecs |

Si le service s'arrête définitivement suite à des crashs répétés, vérifier `stderr.log`
pour identifier la cause avant de le redémarrer.

### Simuler un crash (test de récupération)

```powershell
# Trouver le PID du processus uvicorn
$pid = (Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -eq "" -and $_.Path -like "*AppData\Local\Python*"
} | Select-Object -First 1).Id

# Tuer le processus brutalement
if ($pid) { Stop-Process -Id $pid -Force }

# Attendre 15 secondes puis vérifier que NSSM a redémarré le service
Start-Sleep 15
Get-Service KonektaApp
```

### Désinstaller le service

```powershell
# En tant qu'Administrateur
Stop-Service KonektaApp -Force
& "C:\nssm\nssm-2.24\win64\nssm.exe" remove KonektaApp confirm
```

---

## 5. Limites connues de cette solution

Ces limites sont documentées honnêtement — elles ne sont pas des bugs, mais des
contraintes à prendre en compte pour la gestion des risques.

### Sauvegardes

| Limite | Explication | Mitigation possible |
|--------|-------------|---------------------|
| **Même disque physique** | Les dumps sont sur C: comme la base. Une panne disque détruit les deux. | Brancher un disque USB externe ou configurer un partage réseau comme destination (`$BACKUP_DIR` dans backup_pg.ps1). |
| **Même bâtiment** | Un incendie ou vol du serveur détruit la base et les sauvegardes. | Copie hebdomadaire sur un disque externe emmené hors site, ou upload vers Backblaze B2/rclone. |
| **Pas de sauvegarde temps réel** | Fenêtre de perte de données : jusqu'à 12h entre deux sauvegardes. | Augmenter la fréquence (ex. toutes les 4h) ou activer PostgreSQL WAL archiving pour réplication continue. |
| **Pas d'alerte en cas d'échec** | Le log est écrit, mais personne n'est notifié activement si pg_dump échoue. | Ajouter dans backup_pg.ps1 un envoi d'email via Send-MailMessage ou un appel webhook si la sauvegarde échoue. |

### Service Windows

| Limite | Explication | Mitigation possible |
|--------|-------------|---------------------|
| **Chemin OneDrive** | L'application est dans un dossier OneDrive. Si OneDrive a "Fichiers à la demande" activé et que certains fichiers sont cloud-only, le service SYSTEM ne peut pas les lire. | S'assurer que tous les fichiers du projet sont en "Toujours conserver sur cet appareil" dans les paramètres OneDrive. |
| **Pas de haute disponibilité** | Un seul processus uvicorn, un seul serveur. Si le serveur physique tombe, l'application est indisponible. | Pour aller plus loin : Gunicorn multi-workers, second serveur en standby. Hors de portée pour une beta interne. |
| **Port 8001 sans HTTPS** | Les données transitent en clair sur le réseau. | Installer Nginx ou Caddy en reverse proxy TLS devant le port 8001 (priorité P1 du rapport de maturité). |
| **Compte SYSTEM** | Le service tourne sous SYSTEM (compte Windows le plus puissant). | Pour renforcer : créer un compte de service dédié avec droits minimaux (lecture du répertoire app, connexion locale uniquement). |
| **Pas de health check** | Aucun endpoint `/health` — un moniteur externe ne peut pas vérifier l'état applicatif. | Ajouter `GET /health` dans main.py (30 min de travail). |

---

## 6. Procédure de déménagement du serveur

Si le système est migré vers un nouveau serveur :

```
1. Sur l'ancien serveur : lancer une sauvegarde manuelle (backup_pg.ps1)
2. Copier le dump vers le nouveau serveur
3. Sur le nouveau serveur : installer PostgreSQL 18, Python 3.14+
4. Cloner le dépôt git
5. Copier backend\.env (ne pas utiliser le .env.example — il manque les vraies clés)
6. Recréer la base : psql -U postgres -c "CREATE DATABASE station_db;"
7. Restaurer (voir section 3b)
8. Exécuter les migrations si nécessaire : alembic upgrade head
9. Relancer setup_pgpass.ps1, install_backup_tasks.ps1, install_service.ps1
```
