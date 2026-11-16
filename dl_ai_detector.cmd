@echo off
rem Runs dl_ai_detector from source (after: py -3.13 -m venv .venv, then pip install, see README)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py" %*
