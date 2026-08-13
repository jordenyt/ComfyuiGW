import base64
import json
import os
import random
import time
from urllib.parse import quote, unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import websocket

from comfyui_config import (
    COMFYUI_PATH,
    comfyui_output_dir,
    local_path,
    mode_config_path,
)
from comfyui_images import (
    cleanup_images,
    find_existing_file,
    is_valid_base64_image,
    save_base64_image,
)
import comfyui_process
import comfyui_state as st
from comfyui_process import start_comfyui, stop_comfyui
from comfyui_converter import convert_ui_workflow_to_api  # noqa: F401  (re-exported for callers)
from comfyui_workflow import (
    comfyui_workflow,
    get_workflow_config,
    load_workflow_config,
)
from comfyui_websocket import open_websocket_connection

app = FastAPI()
app.state.max_request_size = 50 * 1024 * 1024  # 50MB limit
templates = Jinja2Templates(directory="templates")


# LORA Models Endpoint
@app.get('/loras')
def get_loras():
    """Get list of LORA model files"""
    loras_dir = os.path.join(COMFYUI_PATH, 'models', 'loras')

    if not os.path.exists(loras_dir):
        raise HTTPException(status_code=404, detail="LORA models directory not found")

    file_paths = []
    for root, _, files in os.walk(loras_dir, followlinks=True):
        real_root = os.path.realpath(root)
        if not real_root.startswith(os.path.realpath(loras_dir)):
            continue

        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, loras_dir)
            file_paths.append(rel_path.replace('\\', '/'))

    return file_paths


# Process Management Endpoints
@app.get("/start_comfyui")
def start_comfyui_endpoint():
    return start_comfyui()


@app.get("/stop_comfyui")
def stop_comfyui_endpoint():
    return stop_comfyui()


# ComfyUI Status Endpoints
@app.get('/comfyui_status')
def comfyui_status():
    return {"progress": st.msg_progress, "step": st.msg_step}


@app.get('/comfyui_interrupt')
def comfyui_interrupt():
    st.msg_progress = "Done"
    if st.promptID:
        import requests
        return requests.post("http://127.0.0.1:8188/interrupt")
    return {}


@app.get('/comfyui_free')
def comfyui_free():
    import requests
    requests.post("http://127.0.0.1:8188/free", json={"unload_models": True, "free_memory": True})
    return {"message": "ComfyUI memory released."}


@app.get('/comfyui_result')
def comfyui_result():
    processed_images_data = []
    for f1 in os.listdir(comfyui_output_dir):
        if f1.startswith("api") and f1.endswith(".jpg"):
            jpg_path = os.path.join(comfyui_output_dir, f1)
            with open(jpg_path, 'rb') as f:
                processed_images_data.append(base64.b64encode(f.read()).decode())
    return {'processed_image': processed_images_data}


@app.get('/find_latest_runflow')
def find_latest_runflow_file():
    """Find the latest file starting with 'runflow' in the comfyui_output_dir"""
    try:
        runflow_files = []

        if os.path.exists(comfyui_output_dir):
            for filename in os.listdir(comfyui_output_dir):
                if filename.startswith("runflow"):
                    filepath = os.path.join(comfyui_output_dir, filename)
                    if os.path.isfile(filepath):
                        mod_time = os.path.getmtime(filepath)
                        runflow_files.append((filename, mod_time, filepath))

        runflow_files.sort(key=lambda x: x[1], reverse=True)

        if runflow_files:
            latest_file = runflow_files[0]
            file_url = f"/download_file/{latest_file[0]}"
            return {
                'file_url': file_url,
                'filename': latest_file[0],
                'filepath': latest_file[2]
            }
        else:
            return {'file_url': None, 'filename': None, 'message': 'No runflow files found'}

    except Exception as e:
        return {'file_url': None, 'filename': None, 'error': str(e)}


@app.get('/find_workflow_outputs/{workflow}')
def find_workflow_outputs(workflow: str):
    """Find all output files for a workflow based on output config"""
    try:
        config = load_workflow_config(workflow)
        output_config = config.get('output', {})

        if 'files' not in output_config:
            return {'files': [], 'has_output_config': False, 'debug': {
                'workflow': workflow,
                'comfyui_output_dir': comfyui_output_dir,
                'workflow_start_time': st.workflow_start_time
            }}

        file_specs = output_config.get('files', [])
        all_files = []

        if os.path.exists(comfyui_output_dir) and st.workflow_start_time:
            for spec in file_specs:
                prefix = spec.get('prefix', 'output')
                extension = spec.get('extension', 'jpg')

                if '/' in prefix or '\\' in prefix:
                    parts = prefix.replace('\\', '/').split('/')
                    subdir = '/'.join(parts[:-1])
                    filename_prefix = parts[-1]
                    search_dir = os.path.join(comfyui_output_dir, subdir)
                else:
                    search_dir = comfyui_output_dir
                    filename_prefix = prefix

                if os.path.exists(search_dir):
                    for filename in os.listdir(search_dir):
                        if filename.startswith(filename_prefix) and filename.endswith(f".{extension}"):
                            filepath = os.path.join(search_dir, filename)
                            mod_time = os.path.getmtime(filepath)
                            if mod_time >= st.workflow_start_time:
                                all_files.append({
                                    'filename': filename,
                                    'filepath': filepath,
                                    'mod_time': mod_time,
                                    'extension': extension,
                                    'prefix': prefix
                                })

        all_files.sort(key=lambda x: x['mod_time'], reverse=True)

        return {
            'files': [{
                'file_url': f"/download_file/{quote(f['filepath'])}",
                'filename': f['filename'],
                'filepath': f['filepath'],
                'extension': f['extension'],
                'mod_time': f['mod_time']
            } for f in all_files],
            'has_output_config': True,
            'debug': {
                'workflow': workflow,
                'config_found': config.get('output'),
                'comfyui_output_dir': comfyui_output_dir,
                'search_dir': search_dir,
                'search_exists': os.path.exists(search_dir),
                'workflow_start_time': st.workflow_start_time,
                'file_specs': file_specs
            }
        }
    except Exception as e:
        return {'files': [], 'has_output_config': False, 'error': str(e)}


@app.get('/download_file')
def download_file(filepath: str = ""):
    """Download a specific file from the output directory"""
    try:
        decoded = unquote(filepath)
        decoded = decoded.replace('/', '\\')
        full_path = os.path.normpath(decoded)
        #print(f"DEBUG download_file: received='{filepath}', decoded='{decoded}', full='{full_path}', exists={os.path.exists(full_path)}")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail=f"File not found: {full_path}")

        return FileResponse(
            full_path,
            media_type='application/octet-stream',
            filename=os.path.basename(full_path)
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_workflow_name(requested):
    """Try <requested>_api first, fall back to the bare name."""
    try:
        load_workflow_config(requested + '_api')
        return requested + '_api'
    except KeyError:
        return requested


@app.post('/comfyui_workflow')
async def comfyui_workflow_handler(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not comfyui_process.comfyui_process:
        raise HTTPException(status_code=503, detail="ComfyUI is not started.")

    if not isinstance(data, dict) or 'workflow' not in data:
        raise HTTPException(status_code=400, detail="Missing 'workflow' key in request body")

    session_id = str(random.randint(1, 1125899906842600))

    try:
        for key, value in data.items():
            if isinstance(value, str) and is_valid_base64_image(value):
                image_bytes = base64.b64decode(value)
                existing_path = find_existing_file(key, image_bytes)
                if existing_path:
                    data[key] = existing_path
                else:
                    cleanup_images([key+"_"])
                    data[key] = save_base64_image(value, key, session_id)

        workflow_name = _resolve_workflow_name(data['workflow'])
        result = comfyui_workflow(workflow_name, data)
        if not isinstance(result, dict) or 'prompt_id' not in result:
            raise HTTPException(status_code=500, detail=f"Failed to get prompt_id from ComfyUI: {result}")
        st.promptID = result['prompt_id']

        if not st.ws:
            open_websocket_connection()

        for f in os.listdir(comfyui_output_dir):
            if f.startswith("api"):
                os.remove(os.path.join(comfyui_output_dir, f))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"comfyui_workflow failed: {e}")

    return {"status": "Got Prompt"}


@app.post('/comfyui_runflow')
async def comfyui_runflow_handler(request: Request):
    form_data = await request.form()

    if not comfyui_process.comfyui_process:
        return {"message": "ComfyUI is not started."}

    workflow_name = form_data.get('workflow')
    if not workflow_name:
        raise HTTPException(status_code=400, detail="Workflow not specified")

    workflow_config = load_workflow_config(workflow_name)

    data = {}
    session_id = str(random.randint(1, 1125899906842600))

    if "inputs" in workflow_config:
        for field_name, field in workflow_config["inputs"].items():
            field_type = field["type"]

            if field_type in ["image", "video", "audio"]:
                file = form_data.get(field_name)
                if file and file.filename:
                    file_content = await file.read()
                    existing_path = find_existing_file(field_name, file_content)
                    if existing_path:
                        data[field_name] = existing_path
                    else:
                        cleanup_images([field_name+"_"])
                        file_ext = os.path.splitext(file.filename)[1]
                        save_path = os.path.join(COMFYUI_PATH, 'input', f"{field_name}_{session_id}{file_ext}")
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                        with open(save_path, 'wb') as f:
                            f.write(file_content)
                        data[field_name] = save_path
            else:
                value = form_data.get(field_name)
                if value is not None:
                    if field_type == "integer":
                        data[field_name] = int(value)
                    elif field_type == "float":
                        data[field_name] = float(value)
                    elif field_type == "array":
                        data[field_name] = [x.strip() for x in value.split(',')]
                    else:
                        data[field_name] = value

        st.promptID = comfyui_workflow(workflow_name, data)['prompt_id']

    if not st.ws:
        open_websocket_connection()

    return templates.TemplateResponse(
        "workflow_progress.html",
        {"request": request, "workflow": workflow_name, "start_time": st.workflow_start_time}
    )


@app.post('/comfyui_caption')
async def comfyui_caption(request: Request):
    data = await request.json()

    if not comfyui_process.comfyui_process:
        return {"caption": ""}

    session_id = str(random.randint(1, 1125899906842600))
    cleanup_images(["background_"])

    if "background" in data:
        data["background"] = save_base64_image(data['background'], "background", session_id)

        caption_file = os.path.join(comfyui_output_dir, "caption.txt")
        if os.path.exists(caption_file):
            os.remove(caption_file)

        workflow_name = _resolve_workflow_name(data['workflow'])
        st.promptID = comfyui_workflow(workflow_name, data)['prompt_id']

        while not os.path.exists(caption_file):
            time.sleep(0.5)

        with open(caption_file, 'r', encoding='utf-8', errors='replace') as file:
            return {"caption": file.read()}

    return {"caption": ""}


@app.get("/mode_config")
async def get_mode_config():
    config = []
    if os.path.exists(mode_config_path):
        with open(mode_config_path, 'r', encoding='utf-8') as f:
            config.extend(json.load(f))
    workflows_dir = os.path.join(local_path, "workflows")
    for root, dirs, files in os.walk(workflows_dir):
        if "mode.config.json" in files:
            config_path = os.path.join(root, "mode.config.json")
            with open(config_path, 'r', encoding='utf-8') as f:
                config.extend(json.load(f))

    if not config:
        raise HTTPException(status_code=404, detail="No config files found")

    return config


@app.get("/runflow", response_class=HTMLResponse)
@app.get("/runflow/{path:path}", response_class=HTMLResponse)
async def list_workflows(request: Request, path: str = None):
    workflows_dir = os.path.join(local_path, "workflows")
    subdirectories = []
    if path:
        full_path = os.path.join(workflows_dir, path)
    else:
        full_path = workflows_dir

    for entry in os.listdir(full_path):
        entry_path = os.path.join(full_path, entry)
        if os.path.isdir(entry_path):
            try:
                sub_path = os.path.join(path, entry) if path else entry
                sub_config = get_workflow_config(sub_path, show_sub=True)
                workflows_with_inputs_in_sub = {
                    name: config for name, config in sub_config.items()
                    if "inputs" in config
                }
                if workflows_with_inputs_in_sub:
                    subdirectories.append(entry)
            except Exception:
                pass

    config = get_workflow_config(path, show_sub=False)

    workflows_with_inputs = {
        name: config for name, config in config.items()
        if "inputs" in config
    }

    current_path = path.replace('\\', '/') if path else ""

    parent_path = None
    if current_path:
        path_parts = current_path.split('/')
        if len(path_parts) > 1:
            parent_path = '/'.join(path_parts[:-1])
        else:
            parent_path = ""

    return templates.TemplateResponse(
        "runflow_list.html",
        {
            "request": request,
            "workflows": workflows_with_inputs,
            "subdirectories": subdirectories,
            "current_path": current_path,
            "parent_path": parent_path
        }
    )


@app.get("/workflow_status", response_class=HTMLResponse)
async def workflow_status_page(request: Request):
    return templates.TemplateResponse(
        "workflow_progress.html",
        {"request": request}
    )


@app.get("/workflow/{workflow}", response_class=HTMLResponse)
async def run_workflow_form(request: Request, workflow: str):
    try:
        workflow_config = load_workflow_config(f"{workflow}_api")

        if "inputs" not in workflow_config:
            raise HTTPException(status_code=400, detail="Workflow has no inputs defined")

        form_fields = []
        for name, field in workflow_config["inputs"].items():
            field_type = field["type"]
            default = field["default"]

            field_def = {
                "name": name,
                "type": field_type,
                "default": default,
                "required": True
            }

            if "choices" in field:
                field_def["choices"] = field["choices"]

            if field_type == "array":
                field_def["item_type"] = "integer"

            form_fields.append(field_def)

        return templates.TemplateResponse(
            "runflow_form.html",
            {
                "request": request,
                "workflow": f"{workflow}_api",
                "form_fields": form_fields
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == '__main__':
    websocket.enableTrace(False)
    start_comfyui()
    uvicorn.run('comfyui_server:app', host='0.0.0.0', port=5000, log_level="warning")
