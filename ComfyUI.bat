@echo off

cd /d %COMFYUI_PATH%
call venv\Scripts\activate
python main.py --fast