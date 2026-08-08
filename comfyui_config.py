import os
import platform

COMFYUI_PATH = os.environ.get('COMFYUI_PATH', os.path.join(os.path.expanduser('~'), 'Workspace', 'ComfyUI'))
local_path = os.path.dirname(os.path.abspath(__file__)) + os.sep
comfyui_output_dir = os.path.join(COMFYUI_PATH, 'output')
workflows_config_path = 'workflows_config.json'
mode_config_path = "mode_config.json"
comfyui_server = "127.0.0.1:8188"
is_windows = platform.system() == 'Windows'
