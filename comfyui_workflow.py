"""Workflow execution: config loading, field overrides, prompt submission."""
import json
import os
import random
import time
from urllib import request

from comfyui_config import comfyui_server, local_path
from comfyui_converter import convert_ui_workflow_to_api
import comfyui_state as st
from comfyui_state import client_id


def set_workflow_value(d, path, key, data):
    for k in path[:-1]:
        d = d.setdefault(k, {})

    if key == "seed":
        d[path[-1]] = random.randint(1, 4294967295)
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
    st.current_prompt = prompt_workflow
    st.node_ids = list(prompt_workflow.keys())
    st.finished_nodes = []
    st.msg_progress = ""
    st.msg_step = ""
    st.last_node = ""
    st.workflow_start_time = time.time()

    p = {"prompt": prompt_workflow, "client_id": client_id}
    headers = {'Content-Type': 'application/json'}
    data = json.dumps(p).encode('utf-8')
    req = request.Request(f"http://{comfyui_server}/prompt", data=data, headers=headers)
    return json.loads(request.urlopen(req).read())


def _prepend_workflows_prefix(workflow_config, workflows_dir, root):
    """Mutate entries whose prompt_workflow is a relative path with no directory.

    The ComfyUI frontend writes ``prompt_workflow`` as just a filename when the
    ``workflows.config.json`` lives in the same directory as the workflow JSON.
    Resolves those to ``workflows/<rel_path>/<file>`` so the file lookup is
    independent of which subdirectory the config was loaded from.
    """
    for wf in workflow_config.values():
        prompt = wf.get("prompt_workflow")
        if prompt and isinstance(prompt, str) and not os.path.isabs(prompt):
            if "/" not in prompt and "\\" not in prompt:
                rel_path = os.path.relpath(root, workflows_dir)
                wf["prompt_workflow"] = os.path.join("workflows", rel_path, wf["prompt_workflow"])


def get_workflow_config(path, show_sub=True):
    safe_path = path if path else ""
    workflows_dir = os.path.join(local_path, "workflows", safe_path)
    config = {}
    if not os.path.exists(workflows_dir):
        return {}

    if show_sub:
        dir_iter = os.walk(workflows_dir)
    else:
        dir_iter = [(workflows_dir, [], os.listdir(workflows_dir))]

    for root, _, files in dir_iter:
        if "workflows.config.json" not in files:
            continue
        config_path = os.path.join(root, "workflows.config.json")
        with open(config_path, 'r', encoding='utf-8') as f:
            workflow_config = json.load(f)
        _prepend_workflows_prefix(workflow_config, workflows_dir, root)
        config.update(workflow_config)
    return config


def load_workflow_config(workflow_name):
    config = get_workflow_config(None, show_sub=True)
    if workflow_name in config:
        return config[workflow_name]
    raise KeyError(f"Workflow '{workflow_name}' not found in any workflows.config.json")


def comfyui_workflow(workflow_name, data):
    workflow_config = load_workflow_config(workflow_name)
    with open(workflow_config['prompt_workflow'], encoding='utf-8') as f:
        prompt_workflow = json.load(f)
    prompt_workflow = convert_ui_workflow_to_api(prompt_workflow)
    prompt_workflow = apply_settings(prompt_workflow, workflow_config['fields'], data)
    return queue_prompt(prompt_workflow)
