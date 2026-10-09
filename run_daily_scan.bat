@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
  call .venv\Scripts\activate
  pip install -r requirements.txt
)
".venv\Scripts\python.exe" jobfinder.py
pause
