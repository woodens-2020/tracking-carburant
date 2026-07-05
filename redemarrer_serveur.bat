@echo off
title Redemarrage Serveur Tracking Carburant
echo ============================================
echo  Redemarrage du serveur Tracking Carburant
echo ============================================
echo.
echo Arret des processus Python en cours...
taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM python3.exe /T 2>nul
timeout /t 2 /nobreak >nul

echo.
echo Demarrage du serveur sur le port 8001...
cd /d "C:\Users\Homilus Woodens\OneDrive\Documents\Tracking-Carburant\backend"
start "Serveur Tracking Carburant" /B python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload

echo.
echo Serveur demarre! Ouverture du navigateur...
timeout /t 3 /nobreak >nul
start http://localhost:8001

echo.
echo Presse une touche pour fermer cette fenetre...
pause >nul
