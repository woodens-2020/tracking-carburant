@echo off
:: Demande automatiquement les droits administrateur
if not "%1"=="am_admin" (
    powershell -Command "Start-Process cmd -ArgumentList '/c \"%~f0\" am_admin' -Verb RunAs -Wait"
    goto :EOF
)

title Redemarrage Serveur Tracking Carburant
cd /d "%~dp0"

echo ============================================
echo  Redemarrage du serveur Tracking Carburant
echo ============================================
echo.

echo [1] Arret de tous les serveurs Python en cours...
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM python3.exe /T 2>nul
taskkill /F /IM uvicorn.exe /T 2>nul

echo [2] Attente 3 secondes...
timeout /t 3 /nobreak >nul

echo [3] Verification que le port 8001 est libre...
netstat -ano | findstr ":8001" >nul 2>&1
if %errorlevel%==0 (
    echo ATTENTION: Le port 8001 est encore utilise. Tentative de liberation...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8001 "') do (
        taskkill /F /PID %%a 2>nul
    )
    timeout /t 2 /nobreak >nul
)

echo [4] Demarrage du serveur sur le port 8001...
cd /d "%~dp0backend"

start "Serveur Tracking Carburant - Port 8001" python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload

echo [5] Attente que le serveur demarre...
timeout /t 5 /nobreak >nul

echo [6] Ouverture du navigateur...
start http://localhost:8001

echo.
echo ============================================
echo  Serveur demarre sur http://localhost:8001
echo  Le CRM est maintenant accessible !
echo ============================================
echo.
pause
