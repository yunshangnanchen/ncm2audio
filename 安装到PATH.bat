@echo off
rem ---------------------------------------------------------------
rem  Add (default) or remove this folder from your user PATH.
rem  After running this, `ncm2audio` works in any new terminal window.
rem  Usage:
rem      安装到PATH.bat            -> add to PATH
rem      安装到PATH.bat remove     -> remove from PATH
rem ---------------------------------------------------------------
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "ACTION=add"
if /i "%~1"=="remove" set "ACTION=remove"

echo [ncm2audio] %ACTION% "%~dp0" ^(--^> user PATH^)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_path.ps1" -Action %ACTION%
echo.
pause
