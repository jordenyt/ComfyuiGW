@echo off

cd /d %COMFYUI_PATH%
call venv\Scripts\activate
SET PATH=%PATH%;C:\temp\ffmpeg\bin
pip install -r requirements.txt
pip cache purge
python main.py --fast