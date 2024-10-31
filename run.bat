@echo off
setlocal

REM Check if the venv folder exists in the current directory
if not exist "venv\" (
    echo No virtual environment found. Creating one...
    
    REM Create a virtual environment in the "venv" folder
    python -m venv venv
    
    REM Activate the virtual environment
    call "venv\Scripts\activate"

    REM Check if requirements.txt exists and install packages
    if exist "requirements.txt" (
        echo Installing packages from requirements.txt...
        pip install -r requirements.txt
    ) else (
        echo requirements.txt not found. No packages will be installed.
    )
) else (
    echo Virtual environment found.
    
    REM Activate the virtual environment
    call "venv\Scripts\activate"
)

REM Run the Python script
echo Starting comfyui_server.py...
python comfyui_server.py