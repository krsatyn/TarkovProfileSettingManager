@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :error
%PY% test_core.py
if errorlevel 1 goto :error
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name TarkovProfileManager --icon "assets\cat.ico" --add-data "assets;assets" tarkov_profile_manager.py
if errorlevel 1 goto :error
echo.
echo EXE: dist\TarkovProfileManager.exe
pause
exit /b 0
:error
echo Build FAILED. Read errors above.
pause
exit /b 1
