#!/bin/bash

# Use provided path or default
COMFYUI_PATH=~/Workspace/ComfyUI

# Change to ComfyUI directory
cd "$COMFYUI_PATH"

# Activate virtual environment
source venv/bin/activate

# Run ComfyUI
python main.py --fast