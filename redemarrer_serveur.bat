@echo off
chcp 65001 >nul
title Konekta — Démarrage du serveur

:: ── Élévation administrateur ────────────────────────────────────────────────
if not "%1"=="am_admin" (
    powershell -Command "Start-Process cmd -ArgumentList '/c \"%~f0\" am_admin' -Verb RunAs -Wait"
    goto :EOF
)

cd /d "%~dp0"

:: ── Couleurs ─────────────────────────────────────────────────────────────────
set ESC=
set CYAN=%ESC%[96m
set GREEN=%ESC%[92m
set YELLOW=%ESC%[93m
set RED=%ESC%[91m
set BOLD=%ESC%[1m
set RESET=%ESC%[0m

cls
echo.
echo  %CYAN%╔══════════════════════════════════════════════════╗%RESET%
echo  %CYAN%║%RESET%         %BOLD%KONEKTA BON PRIX — Démarrage%RESET%            %CYAN%║%RESET%
echo  %CYAN%╚══════════════════════════════════════════════════╝%RESET%
echo.

:: ── Python path ──────────────────────────────────────────────────────────────
set PYTHON="%USERPROFILE%\AppData\Local\Python\bin\python.exe"
if not exist %PYTHON% (
    set PYTHON=python
)

:: ── Étape 1 : Arrêter l'ancienne instance ────────────────────────────────────
echo  %YELLOW%[1/5]%RESET% Arrêt de l'ancienne instance...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM python3.exe /T >nul 2>&1
taskkill /F /IM uvicorn.exe /T  >nul 2>&1
timeout /t 2 /nobreak >nul

:: Libérer le port 8001 si encore occupé
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8001 "') do (
    taskkill /F /PID %%a >nul 2>&1
)
echo  %GREEN%    OK%RESET%

:: ── Étape 2 : Démarrer le serveur ────────────────────────────────────────────
echo  %YELLOW%[2/5]%RESET% Démarrage du serveur FastAPI...
cd /d "%~dp0backend"
start /min "KonektaServeur" %PYTHON% -m uvicorn main:app --host 0.0.0.0 --port 8001
echo  %GREEN%    OK%RESET%

:: ── Étape 3 : Attendre que le serveur réponde ────────────────────────────────
echo  %YELLOW%[3/5]%RESET% Attente que le serveur soit prêt...
set /a ESSAIS=0
:ATTENTE
timeout /t 2 /nobreak >nul
set /a ESSAIS+=1
curl -s -o nul -w "%%{http_code}" http://localhost:8001/ 2>nul | findstr /R "^[23]" >nul
if %errorlevel%==0 goto PRET
if %ESSAIS% geq 15 goto TIMEOUT
goto ATTENTE

:TIMEOUT
echo  %RED%    ERREUR : Le serveur ne répond pas après 30 secondes.%RESET%
echo  %RED%    Vérifiez le fichier backend\.env et les logs.%RESET%
echo.
pause
exit /b 1

:PRET
echo  %GREEN%    OK — Serveur opérationnel !%RESET%

:: ── Étape 4 : Récupérer l'IP locale ─────────────────────────────────────────
echo  %YELLOW%[4/5]%RESET% Détection de l'adresse IP...
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R "IPv4.*192\." 2^>nul') do (
    set IP_LOCAL=%%a
    goto IP_TROUVEE
)
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R "IPv4.*10\." 2^>nul') do (
    set IP_LOCAL=%%a
    goto IP_TROUVEE
)
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R "IPv4.*172\." 2^>nul') do (
    set IP_LOCAL=%%a
    goto IP_TROUVEE
)
set IP_LOCAL= 127.0.0.1
:IP_TROUVEE
set IP_LOCAL=%IP_LOCAL: =%
echo  %GREEN%    IP détectée : %IP_LOCAL%%RESET%

:: ── Étape 5 : Ouvrir le navigateur ───────────────────────────────────────────
echo  %YELLOW%[5/5]%RESET% Ouverture du navigateur...
start http://localhost:8001
echo  %GREEN%    OK%RESET%

:: ── Résumé ────────────────────────────────────────────────────────────────────
echo.
echo  %CYAN%╔══════════════════════════════════════════════════╗%RESET%
echo  %CYAN%║%RESET%  %GREEN%✔  Konekta est démarré avec succès !%RESET%           %CYAN%║%RESET%
echo  %CYAN%╠══════════════════════════════════════════════════╣%RESET%
echo  %CYAN%║%RESET%                                                  %CYAN%║%RESET%
echo  %CYAN%║%RESET%  Sur cet ordinateur :                           %CYAN%║%RESET%
echo  %CYAN%║%RESET%  %BOLD%http://localhost:8001%RESET%                         %CYAN%║%RESET%
echo  %CYAN%║%RESET%                                                  %CYAN%║%RESET%
echo  %CYAN%║%RESET%  Depuis un autre PC du réseau :                 %CYAN%║%RESET%
echo  %CYAN%║%RESET%  %BOLD%http://%IP_LOCAL%:8001%RESET%
echo  %CYAN%║%RESET%                                                  %CYAN%║%RESET%
echo  %CYAN%║%RESET%  Pour arrêter : fermez cette fenêtre            %CYAN%║%RESET%
echo  %CYAN%╚══════════════════════════════════════════════════╝%RESET%
echo.
pause
