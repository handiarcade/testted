@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--diagnostic" goto diagnostic
"%ComSpec%" /d /k ""%~f0" --diagnostic"
exit /b

:diagnostic
title Xbox Downloader et SmartGlass
pushd "%~dp0"
if errorlevel 1 goto dossier_erreur
set "XBOX_RESULT=1"
set "XBOX_LOG=%CD%\xbox_demarrage.log"
>"%XBOX_LOG%" echo Demarrage Xbox Downloader et SmartGlass
if errorlevel 1 goto journal_erreur

for %%F in (xbox_suite.py xbox_app.py xbox_dns_spoof.py xbox_notifications.py xbox_smartglass.py xbox_devtools.py requirements.txt) do if not exist "%%F" goto fichiers_absents

if exist ".venv\Scripts\python.exe" goto verifier_deps
py -3.12 -c "import tkinter" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto essai_py
set "XBOX_PYTHON=py -3.12"
goto creer_venv
:essai_py
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto essai_python
set "XBOX_PYTHON=py -3"
goto creer_venv
:essai_python
python -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto python_absent
set "XBOX_PYTHON=python"
:creer_venv
echo Preparation du dossier Python .venv...
%XBOX_PYTHON% -m venv ".venv" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto echec

:verifier_deps
fc /b "requirements.txt" ".venv\requirements_installees.txt" >nul 2>&1
if errorlevel 1 goto installer
".venv\Scripts\python.exe" -c "import tkinter; from xbox.webapi.api.client import XboxLiveClient" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto installer
goto demarrer

:installer
echo Installation des dependances SmartGlass. Une connexion Internet est necessaire.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "requirements.txt" >>"%XBOX_LOG%" 2>&1
if errorlevel 1 goto echec
copy /y "requirements.txt" ".venv\requirements_installees.txt" >nul
if errorlevel 1 goto echec

:demarrer
echo Ouverture de l'application...
echo Pour HTTP/DNS : utiliser clic droit, Executer en tant qu'administrateur.
echo SmartGlass : connectez votre compte Microsoft depuis l'application.
echo Journal : "%XBOX_LOG%"
".venv\Scripts\python.exe" -u "xbox_suite.py" >>"%XBOX_LOG%" 2>&1
set "XBOX_RESULT=%ERRORLEVEL%"
goto rapport

:fichiers_absents
>>"%XBOX_LOG%" echo Fichiers manquants : extrayez TOUT le ZIP dans un dossier.
>>"%XBOX_LOG%" echo Requis : xbox_suite.py, xbox_app.py, xbox_dns_spoof.py,
>>"%XBOX_LOG%" echo xbox_notifications.py, xbox_smartglass.py, xbox_devtools.py, requirements.txt.
>>"%XBOX_LOG%" echo Verifiez les extensions .py et non .py.txt.
goto rapport

:python_absent
>>"%XBOX_LOG%" echo Installez Python 3.12 avec Tcl/Tk et Python Launcher :
>>"%XBOX_LOG%" echo https://www.python.org/downloads/windows/
goto rapport

:echec
set "XBOX_RESULT=1"
>>"%XBOX_LOG%" echo La preparation a echoue. Consultez l'erreur ci-dessus.
goto rapport

:journal_erreur
echo Impossible d'ecrire dans ce dossier. Deplacez-le vers Documents.
goto fin

:dossier_erreur
echo Impossible d'ouvrir le dossier de l'application.
pause
exit /b 1

:rapport
echo.
type "%XBOX_LOG%"
echo.
echo Code de sortie : %XBOX_RESULT%
:fin
echo La fenetre reste ouverte pour consulter les erreurs.
pause
popd
exit /b %XBOX_RESULT%
