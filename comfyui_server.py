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
import sys

from urllib import request
from fastapi import FastAPI, Request, HTTPException
from PIL import Image
import random

# Read ComfyUI path from environment variable or use default
COMFYUI_PATH = os.environ.get('COMFYUI_PATH', os.path.join(os.path.expanduser('~'), 'Workspace', 'ComfyUI'))

app = FastAPI()
ws = None
comfyui_process = None
local_path = os.path.dirname(__file__) + os.path.sep
comfyui_output_dir = os.path.join(COMFYUI_PATH, 'output')
workflows_config_path = 'workflows_config.json'
mode_config_path = "mode_config.json"
comfyui_server = "127.0.0.1:8188"
promptID = None
client_id = str(uuid.uuid4())
msg_progress = ""
msg_step = ""

# Detect the operating system
is_windows = platform.system() == 'Windows'

@app.get("/mode_config")
async def get_mode_config():
    if os.path.exists(mode_config_path):
        with open(mode_config_path, 'r') as f:
            mode_config = json.load(f)
    else:
        mode_config = None
    if mode_config is not None:
        return mode_config
    else:
        raise HTTPException(status_code=404, detail="Config file not found")

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
                average_time_per_step = elapsed_time / (current_step - last_step)
                remaining_steps = data['max'] - current_step
                estimated_remaining_time = remaining_steps * average_time_per_step
                remaining_time_formatted = format_time(estimated_remaining_time)
                elapsed_time_formatted = format_time(elapsed_time)
                msg_step = '--> Step: ' + str(current_step) + '/' + str(data['max']) + ' [' + elapsed_time_formatted + '<' +remaining_time_formatted + ']'
            else:
                last_node = cur_node
                last_time = time.time()
                last_step = current_step
                msg_step = '--> Step: ' + str(current_step) + '/' + str(data['max'])
        elif message['type'] == 'execution_cached':
            data = message['data']
            for itm in data['nodes']:
              if itm not in finished_nodes:
                  finished_nodes.append(itm)
                  msg_progress = str(len(finished_nodes)) + ' of ' + str(len(node_ids)) + ' nodes processed.'
        elif message['type'] == 'executing':
            data = message['data']
            if data['node'] is None and data['prompt_id'] == promptID:
                msg_progress = "Done"
            elif data['node'] not in finished_nodes:
                finished_nodes.append(data['node'])
                cur_node = data['node']
                msg_progress = "Processing \"" + current_prompt[data['node']]["_meta"]["title"] + "\" (" + str(len(finished_nodes)) + "/" + str(len(node_ids)) + ")..."
        elif message['type'] == 'execution_interrupted':
            data = message['data']
            if data['prompt_id'] == promptID:
                msg_step = ""
                msg_progress = "Done"

    Thread(target=run).start()
    
@app.get('/comfyui_status')
def comfyui_status():
    return {"progress": msg_progress, "step": msg_step}
    
@app.get('/comfyui_result')
def comfyui_result():
    promptID = None
    processed_images_data = []
    for f1 in os.listdir(comfyui_output_dir):
        if f1.startswith("api") and f1.endswith(".jpg"):
            jpg_image_path = os.path.join(comfyui_output_dir, f1)
            with open(jpg_image_path, 'rb') as f:
                processed_image_bytes = f.read()
            processed_image_data = base64.b64encode(processed_image_bytes).decode()
            processed_images_data.append(processed_image_data)

    response = {'processed_image': processed_images_data}
    return response
    
def open_websocket_connection():
    ws = websocket.WebSocketApp("ws://{}/ws?clientId={}".format(comfyui_server, client_id), on_message=ws_message)
    Thread(target=ws.run_forever).start()
    return ws
        
@app.get('/comfyui_interrupt')
def comfyui_interrupt():
    global msg_step, msg_progress
    msg_progress = "Done"
    if promptID is not None:
        return requests.post("http://{}/interrupt".format(comfyui_server))
    return {}
    
@app.get('/comfyui_free')
def comfyui_free():
    response = requests.post("http://{}/free".format(comfyui_server), json={"unload_models":True, "free_memory":True})
    return {"message": "ComfyUI memory released."}

def queue_prompt(prompt_workflow):
    global finished_nodes, node_ids, msg_progress, current_prompt, last_node
    current_prompt = prompt_workflow
    node_ids = list(prompt_workflow.keys())
    finished_nodes = []
    msg_progress = ""
    msg_step = ""
    last_node = ""
    p = {"prompt": prompt_workflow, "client_id": client_id}
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(p).encode('utf-8')
    req =  request.Request("http://{}/prompt".format(comfyui_server), data=data, headers=headers)
    return json.loads(request.urlopen(req).read())

def load_workflow_config(workflow_name):
    with open(workflows_config_path, 'r') as f:
        workflows_config = json.load(f)
    return workflows_config[workflow_name]

def set_workflow_value(d, path, key, data):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    if key == "seed":
        d[path[-1]] = random.randint(1, 1125899906842600)
    elif key == "background":
        d[path[-1]] = background_image_path
    elif key == "reference":
        d[path[-1]] = reference_image_path
    elif key == "paint":
        d[path[-1]] = paint_image_path
    else:
        if key in data:
            d[path[-1]] = data[key]

def apply_settings(prompt_workflow, settings, data):
    for key, value in settings.items():
        if isinstance(settings[key], list):
            for k in settings[key]:
                set_workflow_value(prompt_workflow, k.split("."), key, data)
        else:
            set_workflow_value(prompt_workflow, value.split('.'), key, data)
    return prompt_workflow

def comfyui_workflow(workflow_name, data):
    workflow_config = load_workflow_config(workflow_name)
    prompt_workflow = json.load(open(workflow_config['prompt_workflow']))
    prompt_workflow = apply_settings(prompt_workflow, workflow_config['fields'], data)
    return queue_prompt(prompt_workflow)
        
@app.post('/comfyui_caption')
async def comfyui_caption(request: Request):
    global background_image_path, promptID
    data = await request.json()
    if comfyui_process:
        session_id = str(random.randint(1, 1125899906842600))
        for f in os.listdir(local_path):
            if f.startswith("background_") and f.endswith(".jpg"):
                del_file_path = os.path.join(local_path, f)
                os.remove(del_file_path)
        if "background" in data:
            base64_image_data = data['background']
            decoded_data = base64.b64decode(base64_image_data)
            background_image_path = local_path + "background_" + session_id + ".jpg"
            img_file = open(background_image_path, 'wb')
            img_file.write(decoded_data)
            img_file.close()
            
            filename = "caption.txt"
            filepath = os.path.join(comfyui_output_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
                    
            workflow_name = data['workflow'] + '_api'
            promptID = comfyui_workflow(workflow_name, data)['prompt_id']
            
            while not os.path.exists(filepath):
                time.sleep(0.5)
            
            with open(filepath, 'r') as file:
                content = file.read()
                
            return {"caption":content}
          
    return {"caption":""}
        
@app.post('/comfyui_workflow')
async def comfyui_workflow_handler(request: Request):
    global background_image_path, paint_image_path, reference_image_path, promptID, ws
    data = await request.json()
    if comfyui_process:
        for f in os.listdir(local_path):
            if (f.startswith("background_") or f.startswith("paint_") or f.startswith("reference_")) and f.endswith(".jpg"):
                del_file_path = os.path.join(local_path, f)
                os.remove(del_file_path)
        session_id = str(random.randint(1, 1125899906842600))
        if "background" in data:
            base64_image_data = data['background']
            decoded_data = base64.b64decode(base64_image_data)
            background_image_path = local_path + "background_" + session_id + ".jpg"
            img_file = open(background_image_path, 'wb')
            img_file.write(decoded_data)
            img_file.close()
        if "paint" in data:
            base64_image_data = data['paint']
            decoded_data = base64.b64decode(base64_image_data)
            paint_image_path = local_path + "paint_" + session_id + ".jpg"
            img_file = open(paint_image_path, 'wb')
            img_file.write(decoded_data)
            img_file.close()
        if "reference" in data:
            base64_image_data = data['reference']
            decoded_data = base64.b64decode(base64_image_data)
            reference_image_path = local_path + "reference_" + session_id + ".jpg"
            img_file = open(reference_image_path, 'wb')
            img_file.write(decoded_data)
            img_file.close()

        workflow_name = data['workflow'] + '_api'
        promptID = comfyui_workflow(workflow_name, data)['prompt_id']
        if (ws == None):
            ws = open_websocket_connection()

        # Clean up old files
        for f in os.listdir(comfyui_output_dir):
            if f.startswith("api"):
                del_file_path = os.path.join(comfyui_output_dir, f)
                os.remove(del_file_path)       

        return {"status": "Got Prompt"}
    else:
        return {"message": "ComfyUI is not started."}
            
@app.get("/stop_comfyui")
def stop_comfyui():
    global comfyui_process, ws
    if comfyui_process:
        if ws is not None:
            ws.close()
            ws = None
        
        # Cross-platform process termination
        if is_windows:
            # On Windows, use taskkill for more robust termination
            subprocess.run(f"taskkill /F /T /PID {comfyui_process.pid}", shell=True)
        else:
            # On Linux, use process group termination
            try:
                parent = psutil.Process(comfyui_process.pid)
                for child in parent.children(recursive=True):
                    child.terminate()
                parent.terminate()
            except psutil.NoSuchProcess:
                pass
        
        comfyui_process = None
        return {"message": "ComfyUI Stopped."}
    else:
        return {"message": "ComfyUI not started"}

@app.get("/start_comfyui")        
def start_comfyui():
    global comfyui_process
    if comfyui_process:
        return {"message": "ComfyUI already started"}
    else:
        # Use platform-specific launch script
        if is_windows:
            launch_script = os.path.join(local_path, "ComfyUI.bat")
        else:
            launch_script = os.path.join(local_path, "ComfyUI.sh")
        
        # Environment for subprocess to pass ComfyUI path
        env = os.environ.copy()
        env['COMFYUI_PATH'] = COMFYUI_PATH

        if is_windows:
            # For Windows, use shell=True
            comfyui_process = subprocess.Popen(launch_script, shell=True, env=env)
        else:
            # Make sure the script is executable
            os.chmod(launch_script, 0o755)
            
            # For Linux, use shell=False and shlex.split()
            comfyui_process = subprocess.Popen(
                shlex.split(launch_script), 
                shell=False, 
                start_new_session=True,  # Important for Linux to prevent process termination
                env=env
            )
        
        return {"message": "ComfyUI started"}

if __name__ == '__main__':
    uvicorn.run('comfyui_server:app', host='0.0.0.0', port=5000, log_level="warning")

websocket.enableTrace(False)

start_comfyui()