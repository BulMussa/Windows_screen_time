@echo off
rem Builds dist\ScreenTime.exe. It keeps its logs in %LOCALAPPDATA%\ScreenTime\logs.
rem Needs:  python -m venv .venv  then  .venv\Scripts\pip install pyinstaller cryptography segno
rem (cryptography makes the phone-sync TLS certificate, segno draws the pairing QR code; both go into the .exe)
cd /d "%~dp0.."
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% -c "import cryptography, segno" 2>nul || (echo Missing packages. Run:  %PY% -m pip install cryptography segno & exit /b 1)
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ScreenTime ^
  --collect-submodules cryptography.hazmat.bindings ^
  --icon "%CD%\screentime\assets\screentime.ico" ^
  --add-data "%CD%\screentime\assets;screentime\assets" ^
  --version-file "%CD%\packaging\version_info.txt" ^
  --distpath dist --workpath build --specpath build --paths . ^
  screentime\__main__.py
