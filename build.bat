@echo off
rem Builds ScreenTime.exe next to this file (it keeps its logs in the logs folder here).
rem Needs PyInstaller:  python -m venv .venv  then  .venv\Scripts\pip install pyinstaller
cd /d "%~dp0"
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ScreenTime ^
  --icon "%~dp0assets\screentime.ico" --add-data "%~dp0assets\screentime.ico;assets" ^
  --version-file "%~dp0version_info.txt" ^
  --distpath "%~dp0." --workpath "%~dp0build" --specpath "%~dp0build" ^
  screentime_app.pyw
