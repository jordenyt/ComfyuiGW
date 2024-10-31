# ComfyuiGW
The is a Restful API Gateway for ComfyUI which is designed for running [ComfyUI version of Stable Diffusion Sketch](https://github.com/jordenyt/stable_diffusion_sketch/tree/comfyui).

## Installation on Windows
1. Install ComfyUI.
2. Install ComfyUI Manager and custom nodes.
3. Clone this repo.
4. Update the path of your ComfyUI installation in **comfyui_server.py** by updating the value of `comfyui_output_dir` and `comfyui_server`
5. Execute **run.bat** in a python available console such as Anaconda Prompt.

## Add a workflow
1. Download a ComfyUI workflow (from community) or create a ComfyUI workflow which finishes with a node which save the result images with filename started with `api_` and with extension `.jpg`.
2. Load the above workflow in your ComfyUI installation to check whether it can be correctly run.
3. Put the workflow (ended with `.json`) under `[root]/workflows/`.
4. Update `workflows_config.json` by adding a new entity (see example) under the root JSON Object.
5. Update `mode_config.json` by adding one or more JSON Object (see example) in the root JSON Array.
