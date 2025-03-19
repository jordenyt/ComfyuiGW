@echo off

cd /d %COMFYUI_PATH%
call venv\Scripts\activate
SET PATH=%PATH%;C:\temp\ffmpeg\bin
python main.py --fast