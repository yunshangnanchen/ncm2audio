@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "TOOLDIR=%CD%"
if "%TOOLDIR:~-1%"=="\" set "TOOLDIR=%TOOLDIR:~0,-1%"
set "PYEXE=%~dp0venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0ncm2audio.py" --dir "%TOOLDIR%" %*
if errorlevel 1 pause
