@echo off
rem Builds dist\ScreenTime.exe. It keeps its logs in %LOCALAPPDATA%\ScreenTime\logs.
rem Needs PyInstaller:  python -m venv .venv  then  .venv\Scripts\pip install pyinstaller
cd /d "%~dp0.."
set PY=python
if exist .venv\Scripts\python.exe set PY=.venv\Scripts\python.exe
%PY% -m PyInstaller --noconfirm --onefile --windowed --name ScreenTime ^
  --icon "%CD%\screentime\assets\screentime.ico" ^
  --add-data "%CD%\screentime\assets;screentime\assets" ^
  --version-file "%CD%\packaging\version_info.txt" ^
  --distpath dist --workpath build --specpath build --paths . ^
  screentime\__main__.py
