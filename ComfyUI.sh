#!/bin/bash

# Change to ComfyUI directory
cd "$COMFYUI_PATH"

# Activate virtual environment
source venv/bin/activate

# Run ComfyUI
python main.py --fast