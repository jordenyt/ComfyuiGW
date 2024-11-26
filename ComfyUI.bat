@echo off
set COMFYUI_PATH=E:\Workspace\ComfyUI

cd /d %COMFYUI_PATH%
call venv\Scripts\activate
python main.py --fast