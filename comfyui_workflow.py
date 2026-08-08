import json
import os
import random
import time
from urllib import request
from urllib.parse import quote

import comfyui_state as st
from comfyui_config import comfyui_server, local_path
from comfyui_state import client_id

_node_input_names_cache = {}


def get_node_input_names(node_type):
    """Fetch ordered list of input names for a node type from ComfyUI /object_info.
    Cached. Returns None on failure so callers can fall back gracefully."""
    if node_type in _node_input_names_cache:
        return _node_input_names_cache[node_type]
    try:
        with request.urlopen(
            f"http://{comfyui_server}/object_info/{quote(node_type, safe='')}",
            timeout=5,
        ) as resp:
            data = json.loads(resp.read())
        spec = data.get(node_type, {})
        required = list(spec.get("input", {}).get("required", {}).keys())
        optional = list(spec.get("input", {}).get("optional", {}).keys())
        names = required + optional
    except Exception:
        names = None
    _node_input_names_cache[node_type] = names
    return names


_CONTROL_AFTER_GENERATE_VALUES = {"randomize", "increment", "decrement", "fixed"}

# UI-only widget keys that appear in the workflow's dict-form widgets_values
# but should never be sent to the ComfyUI backend (frontend placeholders).
_UI_WIDGET_KEY_BLACKLIST = {
    "videopreview",
    "choose video to upload",
    "choose image to upload",
}


def _widget_iter(widgets_values):
    """Yield (name, value) pairs from a node's widgets_values, regardless of format.

    Supports both list-style (positional, in INPUT_TYPES order) and
    dict-style (keyed by widget name) representations used by different
    ComfyUI frontend versions.
    """
    if isinstance(widgets_values, dict):
        for name, value in widgets_values.items():
            if name in _UI_WIDGET_KEY_BLACKLIST:
                continue
            yield name, value
        return
    for i, value in enumerate(widgets_values or []):
        yield None, value


def convert_ui_workflow_to_api(workflow):
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
        return workflow

    links = {}
    for link in workflow.get("links", []):
        if isinstance(link, list) and len(link) >= 5:
            links[link[0]] = (link[1], link[2])

    converted = {}
    skipped_types = {"MarkdownNote", "Note"}

    for node in workflow["nodes"]:
        if node.get("mode") == 4:
            continue
        node_type = node.get("type")
        if not node_type or node_type in skipped_types:
            continue

        node_id = str(node.get("id"))
        node_inputs_list = node.get("inputs", []) or []
        widgeted_names = {
            ni["widget"]["name"]
            for ni in node_inputs_list
            if ni.get("widget") and ni.get("widget", {}).get("name")
        }

        connected = {}
        for node_input in node_inputs_list:
            name = node_input.get("name")
            if not name:
                continue
            link_id = node_input.get("link")
            if link_id is not None and link_id in links:
                source_node, source_slot = links[link_id]
                connected[name] = [str(source_node), source_slot]

        widget_pairs = list(_widget_iter(node.get("widgets_values")))

        known_names = get_node_input_names(node_type)
        inputs = {}

        if known_names is None:
            inputs.update(connected)
            for _, value in widget_pairs:
                inputs[f"value{len(inputs)}"] = value
            converted[node_id] = {
                "inputs": inputs,
                "class_type": node_type,
                "_meta": {"title": node.get("title", node_type)},
            }
            continue

        if node.get("widgets_values") is None or isinstance(node.get("widgets_values"), dict):
            for name, value in widget_pairs:
                if name and value is not None:
                    inputs[name] = value
            for name, value in connected.items():
                inputs[name] = value
            converted[node_id] = {
                "inputs": inputs,
                "class_type": node_type,
                "_meta": {"title": node.get("title", node_type)},
            }
            continue

        widgets = [v for _, v in widget_pairs]
        for name in known_names:
            if name in connected:
                inputs[name] = connected[name]
                if name in widgeted_names and widgets:
                    widgets.pop(0)
                continue
            if not widgets:
                continue
            inputs[name] = widgets.pop(0)
            if name == "seed" and widgets and widgets[0] in _CONTROL_AFTER_GENERATE_VALUES:
                widgets.pop(0)

        converted[node_id] = {
            "inputs": inputs,
            "class_type": node_type,
            "_meta": {"title": node.get("title", node_type)},
        }

    return converted


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

    for root, dirs, files in dir_iter:
        if "workflows.config.json" in files:
            config_path = os.path.join(root, "workflows.config.json")
            with open(config_path, 'r', encoding='utf-8') as f:
                workflow_config = json.load(f)
                for name, wf in workflow_config.items():
                    prompt = wf.get("prompt_workflow")
                    if prompt and isinstance(prompt, str) and not os.path.isabs(prompt):
                        if "/" not in prompt and "\\" not in prompt:
                            rel_path = os.path.relpath(root, workflows_dir)
                            wf["prompt_workflow"] = os.path.join("workflows", rel_path, wf["prompt_workflow"])
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
