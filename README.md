# ComfyuiGW
The is a Restful API Gateway for ComfyUI which is designed for running [ComfyUI version of Stable Diffusion Sketch](https://github.com/jordenyt/stable_diffusion_sketch/tree/comfyui).

## Installation on Windows
1. Install [ComfyUI](https://github.com/comfyanonymous/ComfyUI).
2. Install all of below modules by cloning them under `/custom_nodes` in ComfyUI root folder.
   * [ComfyUI Manager](https://github.com/ltdrdata/ComfyUI-Manager)
   * [kijai/ComfyUI-Florence2](https://github.com/kijai/ComfyUI-Florence2)
   * [pythongosssss/ComfyUI-Custom-Scripts](https://github.com/pythongosssss/ComfyUI-Custom-Scripts)
   * [kijai/ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes)
   * [WASasquatch/was-node-suite-comfyui](https://github.com/WASasquatch/was-node-suite-comfyui)
   * [BlenderNeko/ComfyUI_ADV_CLIP_emb](https://github.com/BlenderNeko/ComfyUI_ADV_CLIP_emb)
   * [jamesWalker55/comfyui-various](https://github.com/jamesWalker55/comfyui-various)
   * [Fannovel16/comfyui_controlnet_aux](https://github.com/Fannovel16/comfyui_controlnet_aux)
4. Clone this repo.
5. Update the path of your ComfyUI installation in **ComfyUI.bat**(Windows) or **Comfyui.sh**(Linux) by updating the value of `COMFYUI_PATH`.
7. Execute **run.bat**(Windows) or **run.sh**(Linux) in a python available console such as Anaconda Prompt.

## Add a workflow
1. Download a ComfyUI workflow (from [SDSketch Discussions](https://github.com/jordenyt/stable_diffusion_sketch/discussions)) or create a ComfyUI workflow which finishes with a node which save the result images with filename started with `api_` and with extension `.jpg`.
2. Load the above workflow in your ComfyUI installation to check whether it can be correctly run.
3. Put the workflow (ended with `.json`) under `[root]/workflows/`.
4. Update `workflows_config.json` by adding a new entity (see example) under the root JSON Object.
5. Update `mode_config.json` by adding one or more JSON Object (see example) in the root JSON Array.
