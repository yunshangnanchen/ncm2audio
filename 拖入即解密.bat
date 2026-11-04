@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=%~dp0venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0ncm2audio.py" %* --no-pause
echo.
pause
