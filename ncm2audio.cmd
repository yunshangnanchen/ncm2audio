@echo off
rem ---------------------------------------------------------------
rem  ncm2audio global launcher
rem  Add this folder to PATH, then run `ncm2audio` from ANY directory.
rem  It scans the CURRENT working directory, never the install folder.
rem  Only ASCII text here on purpose: .cmd files must survive any codepage.
rem ---------------------------------------------------------------
setlocal

rem --- locate this script even when invoked through PATH (see below) ---
set "SELF=%~f0"
if not exist "%SELF%" for %%I in ("%~nx0") do set "SELF=%%~$PATH:I"
if not exist "%SELF%" set "SELF=%~dp0%~nx0"
for %%I in ("%SELF%") do set "ROOT=%%~dpI"

rem --- remember console codepage, switch to UTF-8 for Chinese output ---
set "OLDCP="
for /f "tokens=2 delims=:" %%A in ('chcp') do set "OLDCP=%%A"
set "OLDCP=%OLDCP: =%"
chcp 65001 >nul 2>nul

rem --- pick an interpreter: project venv > .venv > py -3 > python ---
if exist "%ROOT%venv\Scripts\python.exe"  goto :use_venv
if exist "%ROOT%.venv\Scripts\python.exe" goto :use_dotvenv
where py >nul 2>nul
if not errorlevel 1 goto :use_py
python -c "" >nul 2>nul
if not errorlevel 1 goto :use_python

echo [ncm2audio] Python not found. Install Python 3.9+ and check "Add python.exe to PATH",
echo              or run the setup script (install-deps) in "%ROOT%" once.
set "RC=1"
goto :done

:use_venv
"%ROOT%venv\Scripts\python.exe" "%ROOT%ncm2audio.py" %*
set "RC=%ERRORLEVEL%"
goto :done

:use_dotvenv
"%ROOT%.venv\Scripts\python.exe" "%ROOT%ncm2audio.py" %*
set "RC=%ERRORLEVEL%"
goto :done

:use_py
py -3 "%ROOT%ncm2audio.py" %*
set "RC=%ERRORLEVEL%"
goto :done

:use_python
python "%ROOT%ncm2audio.py" %*
set "RC=%ERRORLEVEL%"
goto :done

:done
if defined OLDCP chcp %OLDCP% >nul 2>nul
exit /b %RC%
