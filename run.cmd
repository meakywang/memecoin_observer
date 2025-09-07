@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call .venv\Scripts\python.exe -m pip install -r requirements.txt
call .venv\Scripts\python.exe -X utf8 -m app.main
