import base64
import subprocess
import time
import uvicorn
import psutil
import json
import os
import websocket
from threading import Thread
import uuid
import requests
import platform
import shlex
from urllib import request
from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import random
from io import BytesIO
from PIL import Image

# Configuration
COMFYUI_PATH = os.environ.get('COMFYUI_PATH', os.path.join(os.path.expanduser('~'), 'Workspace', 'ComfyUI'))
local_path = os.path.dirname(__file__) + os.path.sep
comfyui_output_dir = os.path.join(COMFYUI_PATH, 'output')
workflows_config_path = 'workflows_config.json'
mode_config_path = "mode_config.json"
comfyui_server = "127.0.0.1:8188"
is_windows = platform.system() == 'Windows'

# Global variables
app = FastAPI()
app.state.max_request_size = 50 * 1024 * 1024  # 50MB limit
templates = Jinja2Templates(directory="templates")
ws = None
comfyui_process = None
promptID = None
client_id = str(uuid.uuid4())
msg_progress = ""
msg_step = ""
current_prompt = None
finished_nodes = []
node_ids = []
last_node = ""
last_time = 0
last_step = 0
cur_node = ""

# Helper functions
def format_time(seconds):
    """Convert seconds to mm:ss format."""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def get_workflow_config():
    config = {}
    if os.path.exists(workflows_config_path):
        with open(workflows_config_path, 'r') as f:
            config.update(json.load(f))
    workflows_dir = os.path.join(local_path, "workflows")
    for root, dirs, files in os.walk(workflows_dir):
        if "workflows.config.json" in files:
            config_path = os.path.join(root, "workflows.config.json")
            with open(config_path, 'r') as f:
                workflow_config = json.load(f)
                # For each workflow in this config, check if prompt_workflow needs path prepended
                for name, wf in workflow_config.items():
                    if "prompt_workflow" in wf and not os.path.isabs(wf["prompt_workflow"]):
                        if "/" not in wf["prompt_workflow"] and "\\" not in wf["prompt_workflow"]:
                            # Get relative path from workflows_dir to current config file
                            rel_path = os.path.relpath(root, workflows_dir)
                            wf["prompt_workflow"] = os.path.join("workflows", rel_path, wf["prompt_workflow"])
                config.update(workflow_config)
    if not config:
        raise KeyError("No workflows.config.json files found in workflows directory")
    return config
    
def load_workflow_config(workflow_name):
    config = get_workflow_config()
    if workflow_name in config:
        return config[workflow_name]
    raise KeyError(f"Workflow '{workflow_name}' not found in any workflows.config.json")

def set_workflow_value(d, path, key, data):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    
    if key == "seed":
        d[path[-1]] = random.randint(1, 1125899906842600)
    elif key in data:
        d[path[-1]] = data[key]

def apply_settings(prompt_workflow, settings, data):
    for key, value in settings.items():
        if isinstance(settings[key], list):
            for k in settings[key]:
                set_workflow_value(prompt_workflow, k.split("."), key, data)
        else:
            set_workflow_value(prompt_workflow, value.split('.'), key, data)
    return prompt_workflow

def queue_prompt(prompt_workflow):
    global finished_nodes, node_ids, msg_progress, current_prompt, last_node
    current_prompt = prompt_workflow
    node_ids = list(prompt_workflow.keys())
    finished_nodes = []
    msg_progress = ""
    global msg_step
    msg_step = ""
    last_node = ""
    
    p = {"prompt": prompt_workflow, "client_id": client_id}
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(p).encode('utf-8')
    req = request.Request(f"http://{comfyui_server}/prompt", data=data, headers=headers)
    return json.loads(request.urlopen(req).read())

def comfyui_workflow(workflow_name, data):
    workflow_config = load_workflow_config(workflow_name)
    prompt_workflow = json.load(open(workflow_config['prompt_workflow']))
    prompt_workflow = apply_settings(prompt_workflow, workflow_config['fields'], data)
    return queue_prompt(prompt_workflow)

def open_websocket_connection():
    ws = websocket.WebSocketApp(f"ws://{comfyui_server}/ws?clientId={client_id}", on_message=ws_message)
    Thread(target=ws.run_forever).start()
    return ws

def is_valid_base64_image(base64_data):
    try:
        decoded_data = base64.b64decode(base64_data)
    except (base64.binascii.Error, ValueError, TypeError):
        return False

    try:
        with Image.open(BytesIO(decoded_data)) as img:
            img.verify()  # Basic verification
            img = Image.open(BytesIO(decoded_data))
            img.getdata()[0]  # Try to access pixel data
        return True
    except Exception:
        return False

def save_base64_image(base64_data, prefix, session_id):
    decoded_data = base64.b64decode(base64_data)
    save_path = f"{local_path}inputs{os.path.sep}{prefix}_{session_id}.jpg"
    with open(save_path, 'wb') as img_file:
        img_file.write(decoded_data)
    return save_path

def cleanup_images(prefixes):
    inputs_path = os.path.join(local_path, "inputs")
    if os.path.exists(inputs_path):
        for f in os.listdir(inputs_path):
            for prefix in prefixes:
                if f.startswith(prefix) and (f.endswith(".jpg") or f.endswith(".mp4") or f.endswith(".mp3") or f.endswith(".wav")):
                    os.remove(os.path.join(inputs_path, f))

def ws_message(ws, msg):
    def run(*args):
        global finished_nodes, node_ids, msg_step, msg_progress, cur_node, last_node, last_time, last_step
        message = json.loads(msg)
        
        if message['type'] == 'progress':
            data = message['data']
            current_step = data['value']
            if current_step == data['max']:
                msg_step = ""
            elif last_node == cur_node:
                elapsed_time = time.time() - last_time
                avg_time_per_step = elapsed_time
                if current_step > last_step:
                    avg_time_per_step = elapsed_time / (current_step - last_step)
                elif current_step > 0:
                    avg_time_per_step = elapsed_time / current_step
                remaining_steps = data['max'] - current_step
                estimated_time = remaining_steps * avg_time_per_step
                msg_step = f"--> Step: {current_step}/{data['max']} [{format_time(elapsed_time)}<{format_time(estimated_time)}]"
            else:
                last_node = cur_node
                last_time = time.time()
                last_step = current_step
                msg_step = f"--> Step: {current_step}/{data['max']}"
                
        elif message['type'] == 'execution_cached':
            data = message['data']
            for itm in data['nodes']:
                if itm not in finished_nodes:
                    finished_nodes.append(itm)
                    msg_progress = f"{len(finished_nodes)} of {len(node_ids)} nodes processed."
                    
        elif message['type'] == 'executing':
            data = message['data']
            if data['node'] is None and data['prompt_id'] == promptID:
                msg_progress = "Done"
            elif data['node'] not in finished_nodes:
                finished_nodes.append(data['node'])
                cur_node = data['node']
                msg_progress = f"Processing \"{current_prompt[data['node']]['_meta']['title']}\" ({len(finished_nodes)}/{len(node_ids)})..."
                
        elif message['type'] == 'execution_interrupted':
            data = message['data']
            if data['prompt_id'] == promptID:
                msg_step = ""
                msg_progress = "Done"

    Thread(target=run).start()

# Process Management Endpoints
@app.get("/start_comfyui")        
def start_comfyui():
    global comfyui_process
    if comfyui_process:
        return {"message": "ComfyUI already started"}
    
    # Use platform-specific launch script
    launch_script = os.path.join(local_path, "ComfyUI.bat" if is_windows else "ComfyUI.sh")
    env = os.environ.copy()
    env['COMFYUI_PATH'] = COMFYUI_PATH
    
    if is_windows:
        comfyui_process = subprocess.Popen(launch_script, shell=True, env=env)
    else:
        os.chmod(launch_script, 0o755)
        comfyui_process = subprocess.Popen(
            shlex.split(launch_script), 
            shell=False, 
            start_new_session=True,
            env=env
        )
    
    return {"message": "ComfyUI started"}

@app.get("/stop_comfyui")
def stop_comfyui():
    global comfyui_process, ws
    if not comfyui_process:
        return {"message": "ComfyUI not started"}
    
    if ws:
        ws.close()
        ws = None
    
    # Cross-platform process termination
    if is_windows:
        subprocess.run(f"taskkill /F /T /PID {comfyui_process.pid}", shell=True)
    else:
        try:
            parent = psutil.Process(comfyui_process.pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
        except psutil.NoSuchProcess:
            pass
    
    comfyui_process = None
    return {"message": "ComfyUI Stopped."}

# ComfyUI API Endpoints
@app.get('/comfyui_status')
def comfyui_status():
    return {"progress": msg_progress, "step": msg_step}

@app.get('/comfyui_interrupt')
def comfyui_interrupt():
    global msg_step, msg_progress
    msg_progress = "Done"
    if promptID:
        return requests.post(f"http://{comfyui_server}/interrupt")
    return {}

@app.get('/comfyui_free')
def comfyui_free():
    requests.post(f"http://{comfyui_server}/free", json={"unload_models": True, "free_memory": True})
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

@app.post('/comfyui_workflow')
async def comfyui_workflow_handler(request: Request):
    global promptID, ws  # Changed to use dictionary
    data = await request.json()
    
    if not comfyui_process:
        return {"message": "ComfyUI is not started."}
    
    session_id = str(random.randint(1, 1125899906842600))
    
    for key, value in data.items():
        if isinstance(value, str) and is_valid_base64_image(value):
            #print(f"{key} is_valid_base64_image.")
            cleanup_images([key+"_"])
            data[key] = save_base64_image(value, key, session_id)

    workflow_name = data['workflow'] + '_api'
    promptID = comfyui_workflow(workflow_name, data)['prompt_id']
    
    if not ws:
        ws = open_websocket_connection()

    # Clean up old files
    for f in os.listdir(comfyui_output_dir):
        if f.startswith("api"):
            os.remove(os.path.join(comfyui_output_dir, f))

    return {"status": "Got Prompt"}

@app.post('/comfyui_runflow')
async def comfyui_runflow_handler(request: Request):
    global promptID, ws
    form_data = await request.form()
    
    if not comfyui_process:
        return {"message": "ComfyUI is not started."}
    
    # Get workflow config
    workflow_name = form_data.get('workflow')
    if not workflow_name:
        raise HTTPException(status_code=400, detail="Workflow not specified")
    
    workflow_config = load_workflow_config(workflow_name)
    
    # Process form data
    data = {}
    #cleanup_images([])
    session_id = str(random.randint(1, 1125899906842600))
    
    if "inputs" in workflow_config:
        for field_name, field in workflow_config["inputs"].items():
            field_type = field["type"]
            
            if field_type in ["image", "video", "audio"]:
                # Handle file upload
                cleanup_images([field_name+"_"])
                file = form_data.get(field_name)
                if file and file.filename:
                    # Save file
                    file_ext = os.path.splitext(file.filename)[1]
                    save_path = f"{local_path}inputs{os.path.sep}{field_name}_{session_id}{file_ext}"
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, 'wb') as f:
                        f.write(await file.read())
                    data[field_name] = save_path
            else:
                # Handle regular fields
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
    
    # Run workflow
    promptID = comfyui_workflow(workflow_name, data)['prompt_id']
    
    if not ws:
        ws = open_websocket_connection()


    return templates.TemplateResponse(
        "workflow_progress.html", 
        {"request": request}
    )

@app.post('/comfyui_caption')
async def comfyui_caption(request: Request):
    global promptID
    data = await request.json()
    
    if not comfyui_process:
        return {"caption": ""}
    
    session_id = str(random.randint(1, 1125899906842600))
    cleanup_images(["background_"])
    
    if "background" in data:
        data["background"]=save_base64_image(data['background'], "background", session_id)
        
        caption_file = os.path.join(comfyui_output_dir, "caption.txt")
        if os.path.exists(caption_file):
            os.remove(caption_file)
                
        workflow_name = data['workflow'] + '_api'
        promptID = comfyui_workflow(workflow_name, data)['prompt_id']
        
        while not os.path.exists(caption_file):
            time.sleep(0.5)
        
        with open(caption_file, 'r', encoding='utf-8', errors='replace') as file:
            return {"caption": file.read()}
              
    return {"caption": ""}

@app.get("/mode_config")
async def get_mode_config():
    config = []
    # First check root mode_config.json if it exists
    if os.path.exists(mode_config_path):
        with open(mode_config_path, 'r') as f:
            config.extend(json.load(f))
    # Scan workflows directory for mode.config.json files
    workflows_dir = os.path.join(local_path, "workflows")
    for root, dirs, files in os.walk(workflows_dir):
        if "mode.config.json" in files:
            config_path = os.path.join(root, "mode.config.json")
            with open(config_path, 'r') as f:
                config.extend(json.load(f))
    
    if not config:
        raise HTTPException(status_code=404, detail="No config files found")
    
    return config

@app.get("/runflow", response_class=HTMLResponse)
async def list_workflows(request: Request):
    config = get_workflow_config()
    
    # Filter workflows to only those with inputs defined
    workflows_with_inputs = {
        name: config for name, config in config.items() 
        if "inputs" in config
    }
    
    return templates.TemplateResponse(
        "runflow_list.html",
        {
            "request": request,
            "workflows": workflows_with_inputs
        }
    )

@app.get("/workflow_status", response_class=HTMLResponse)
async def workflow_status_page(request: Request):
    return templates.TemplateResponse(
        "workflow_progress.html",
        {"request": request}
    )

@app.get("/runflow/{workflow}", response_class=HTMLResponse)
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
            
            if field_type == "array":
                field_def["item_type"] = "integer"  # Assuming array contains integers
                
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
    uvicorn.run('comfyui_server:app', host='0.0.0.0', port=5000, log_level="warning")
    
websocket.enableTrace(False)
start_comfyui()
