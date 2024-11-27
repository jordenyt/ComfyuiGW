#!/bin/bash

# Check if the venv folder exists in the current directory
if [ ! -d "venv" ]; then
    echo "No virtual environment found. Creating one..."
    
    # Create a virtual environment in the "venv" folder
    python3 -m venv venv
    
    # Activate the virtual environment
    source venv/bin/activate
    
    # Check if requirements.txt exists and install packages
    if [ -f "requirements.txt" ]; then
        echo "Installing packages from requirements.txt..."
        pip install -r requirements.txt
    else
        echo "requirements.txt not found. No packages will be installed."
    fi
else
    echo "Virtual environment found."
    
    # Activate the virtual environment
    source venv/bin/activate
fi

# Use provided path or default
export COMFYUI_PATH=~/Workspace/ComfyUI

# Run the Python script
echo "Starting comfyui_server.py..."
python3 comfyui_server.py