@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Checking Python ...
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python was not found.
    echo Please install Python 3.9+ from https://www.python.org/downloads/
    echo and remember to check "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo [2/3] Creating virtual environment in .\venv ...
    python -m venv venv --without-pip
    python -m pip install --disable-pip-version-check -q --target venv\Lib\site-packages pip -i https://pypi.tuna.tsinghua.edu.cn/simple
) else (
    echo [2/3] Virtual environment already exists, skipping creation.
)

echo [3/3] Installing dependencies (pillow / pygame-ce / mutagen) ...
venv\Scripts\python.exe -m pip install --disable-pip-version-check -q pillow pygame-ce mutagen -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo.
    echo Install failed. You can retry manually with another mirror, for example:
    echo   venv\Scripts\python.exe -m pip install pillow pygame-ce mutagen
    echo.
    pause
    exit /b 1
)

echo.
echo Done.
echo   * Drag .ncm files or folders onto the decrypt bat in this folder.
echo   * Double-click the player bat in this folder to open the terminal player.
echo.
pause
