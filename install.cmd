@echo off
cd /d "%~dp0"
set "BRIEF_PYTHON=python"
if exist "%LOCALAPPDATA%\NoticeDevTools\python-3.13.17\tools\python.exe" set "BRIEF_PYTHON=%LOCALAPPDATA%\NoticeDevTools\python-3.13.17\tools\python.exe"
"%BRIEF_PYTHON%" -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest -v
if errorlevel 1 goto failed
.venv\Scripts\python.exe setup.py
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Setup stopped. No success or delivery is implied.
pause
exit /b 1
