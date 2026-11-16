@echo off
title NOX AUTO TRADER 2.0
cd /d "%~dp0"
echo ========================================
echo       NOX AUTO TRADER 2.0 - START
echo ========================================
echo.
python --version
if errorlevel 1 (
 echo Python hittades inte. Kontrollera installationen.
 pause
 exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
 echo Paketinstallationen misslyckades. Skicka en bild pa felet.
 pause
 exit /b 1
)
echo.
echo Startar NOX AUTO TRADER 2.0...
echo Oppna http://localhost:8501 om webblasaren inte oppnas.
python -m streamlit run app.py
pause
