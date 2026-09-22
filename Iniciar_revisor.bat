@echo off
rem Abre el revisor de declaraciones en el navegador. Doble clic y listo.
cd /d "%~dp0"
title Revisor de declaraciones de importacion
where python >nul 2>nul
if errorlevel 1 (
  echo No se encontro Python. Instalalo desde https://www.python.org/downloads/ marcando "Add python to PATH".
  pause
  exit /b 1
)
python -c "import pypdf" >nul 2>nul
if errorlevel 1 (
  echo Instalando el componente que lee los PDF ^(solo la primera vez^)...
  python -m pip install -r requirements.txt
)
python interfaz.py
pause
