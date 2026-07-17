<#
Autorise Chrome et Edge a traiter les adresses Konekta (en HTTP simple, sans certificat)
comme des origines "securisees", afin que la geolocalisation et la camera du navigateur
fonctionnent malgre l'absence de HTTPS.

A executer en Administrateur sur CHAQUE poste employe qui doit se connecter a Konekta
(pas seulement sur le serveur). Redemarrer completement Chrome/Edge (fermer toutes les
fenetres) apres l'execution pour que la regle prenne effet.

Solution temporaire tant que le HTTPS n'est pas mis en place sur le serveur.
#>

$origins = @(
    "http://192.168.0.49:8001",
    "http://192.168.0.49.nip.io:8001",
    "http://192.168.0.50:8001",
    "http://192.168.0.50.nip.io:8001",
    "http://100.118.114.21:8001",
    "http://100.118.114.21.nip.io:8001"
)

function Set-InsecureOriginPolicy {
    param(
        [string]$BasePath,
        [string]$BrowserName
    )
    $policyKey = Join-Path $BasePath "OverrideSecurityRestrictionsOnInsecureOrigin"
    New-Item -Path $policyKey -Force | Out-Null
    for ($i = 0; $i -lt $origins.Count; $i++) {
        New-ItemProperty -Path $policyKey -Name ([string]($i + 1)) -Value $origins[$i] -PropertyType String -Force | Out-Null
    }
    Write-Output "$BrowserName configure ($($origins.Count) origines) : $policyKey"
}

Set-InsecureOriginPolicy -BasePath "HKLM:\SOFTWARE\Policies\Google\Chrome" -BrowserName "Chrome"
Set-InsecureOriginPolicy -BasePath "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -BrowserName "Edge"

Write-Output ""
Write-Output "Termine. Fermer TOUTES les fenetres Chrome/Edge puis les rouvrir pour appliquer la regle."
Write-Output "Verification : ouvrir chrome://policy (ou edge://policy) -> chercher OverrideSecurityRestrictionsOnInsecureOrigin."
