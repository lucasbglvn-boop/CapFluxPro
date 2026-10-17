@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set PYTHONUTF8=1

echo ==== CapFlux: dong goi thanh app desktop ====

where python >nul 2>nul || (echo [LOI] Chua cai Python 3.10+ & pause & exit /b 1)

if not exist venv-build\Scripts\python.exe python -m venv venv-build
call venv-build\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements-desktop.txt -q || (echo [LOI] Cai thu vien that bai & pause & exit /b 1)

rem ---- Tai ffmpeg + yt-dlp vao bin\ (chi tai lan dau) ----
if not exist bin mkdir bin
if not exist bin\yt-dlp.exe (
    echo [Setup] Tai yt-dlp.exe...
    curl -L -o bin\yt-dlp.exe https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe
)
if not exist bin\ffmpeg.exe (
    echo [Setup] Tai ffmpeg ^(~90MB^)...
    curl -L -o ffmpeg.zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
    powershell -NoProfile -Command "Expand-Archive -Force ffmpeg.zip ffmpeg_tmp; Get-ChildItem ffmpeg_tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1 | Copy-Item -Destination bin\ffmpeg.exe"
    rmdir /s /q ffmpeg_tmp 2>nul
    del ffmpeg.zip 2>nul
)
if not exist bin\ffmpeg.exe (echo [LOI] Thieu bin\ffmpeg.exe - hay tu copy vao thu muc bin & pause & exit /b 1)

set ICON=
if exist assets\icon.ico set ICON=--icon assets\icon.ico

echo [Build] PyInstaller...
pyinstaller --noconfirm --clean --windowed --name CapFlux %ICON% ^
  --add-data "index.html;web" ^
  --hidden-import main --hidden-import bigo_engine ^
  --collect-submodules uvicorn ^
  --collect-submodules Crypto ^
  --collect-all curl_cffi ^
  --hidden-import http.server --hidden-import urllib.parse --hidden-import multipart --hidden-import psutil --hidden-import requests --hidden-import socks ^
  --exclude-module tkinter ^
  desktop.py || (echo [LOI] Build that bai & pause & exit /b 1)

echo [Build] Copy bin\ canh CapFlux.exe...
xcopy /E /I /Y bin dist\CapFlux\bin >nul

echo.
echo ==== XONG ====
echo Chay thu: dist\CapFlux\CapFlux.exe
echo Gui cho nguoi khac: nen thu muc dist\CapFlux thanh .zip
pause
