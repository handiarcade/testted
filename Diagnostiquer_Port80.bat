@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--diagnostic" goto diagnostic
"%ComSpec%" /d /k ""%~f0" --diagnostic"
exit /b

:diagnostic
title Diagnostic du port 80 - Xbox Downloader
pushd "%~dp0"
if errorlevel 1 goto dossier_erreur
set "XBOX_LOG=%CD%\diagnostic_port80.txt"
set "XBOX_CAPTURE=%TEMP%\xbox_port80_%RANDOM%_%RANDOM%.txt"
echo Fermez Xbox Downloader avant le diagnostic.
echo Clic droit, Executer en tant qu'administrateur, pour des resultats complets.
echo Aucun service ni reglage Windows ne sera modifie.
if not exist "Diagnostiquer_Port80.ps1" goto secours_absent

powershell.exe -NoLogo -NoProfile -File "%~dp0Diagnostiquer_Port80.ps1" >"%XBOX_CAPTURE%" 2>&1
set "XBOX_RESULT=%ERRORLEVEL%"
type "%XBOX_CAPTURE%"
if not "%XBOX_RESULT%"=="0" goto secours
del "%XBOX_CAPTURE%" >nul 2>&1
goto fin

:secours_absent
>"%XBOX_CAPTURE%" echo Le fichier Diagnostiquer_Port80.ps1 est absent.
:secours
echo.
echo PowerShell indisponible, script refuse par sa strategie ou erreur de diagnostic.
echo La strategie PowerShell n'est pas modifiee.
echo Execution des commandes CMD de lecture seule.
>"%XBOX_LOG%" echo Diagnostic CMD du port 80 - Xbox Downloader
if errorlevel 1 goto journal_erreur
type "%XBOX_CAPTURE%" >>"%XBOX_LOG%" 2>&1
>>"%XBOX_LOG%" echo.
>>"%XBOX_LOG%" echo Ecoutes ou connexions TCP avec port LOCAL 80 :
netstat -ano -p tcp | findstr /R /C:":80 .*LISTENING" /C:":80 .*ECOUTE" >>"%XBOX_LOG%" 2>&1
>>"%XBOX_LOG%" echo.
>>"%XBOX_LOG%" echo Plages TCP exclues IPv4 :
netsh interface ipv4 show excludedportrange protocol=tcp >>"%XBOX_LOG%" 2>&1
>>"%XBOX_LOG%" echo.
>>"%XBOX_LOG%" echo Plages TCP exclues IPv6 :
netsh interface ipv6 show excludedportrange protocol=tcp >>"%XBOX_LOG%" 2>&1
>>"%XBOX_LOG%" echo.
>>"%XBOX_LOG%" echo Adresses d'ecoute HTTP.sys :
netsh http show iplisten >>"%XBOX_LOG%" 2>&1
>>"%XBOX_LOG%" echo.
>>"%XBOX_LOG%" echo WinError 10048 : port occupe. WinError 10013 : liaison refusee, pas forcement un manque de droits administrateur.
>>"%XBOX_LOG%" echo Si le port 80 est reserve ou occupe, ne pas supprimer de reservation ni terminer de processus automatiquement.
>>"%XBOX_LOG%" echo Cette solution CMD n'effectue pas les tests temporaires TcpListener80/8080 du script PowerShell.
type "%XBOX_LOG%"
echo.
echo Capture PowerShell disponible aussi dans : "%XBOX_CAPTURE%"

:fin
echo.
echo Journal a transmettre : "%XBOX_LOG%"
echo La fenetre reste ouverte.
pause
popd
exit /b 0

:journal_erreur
echo Impossible de creer le journal dans ce dossier.
echo La sortie PowerShell reste dans : "%XBOX_CAPTURE%"
pause
popd
exit /b 2

:dossier_erreur
echo Impossible d'ouvrir le dossier du diagnostic.
pause
exit /b 1
