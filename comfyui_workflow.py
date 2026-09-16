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
        # Skip fields the caller didn't supply (and aren't the auto-random seed).
        # Without this, set_workflow_value's setdefault would create empty
        # intermediate node entries for unset optional fields whose path targets
        # nodes that no longer exist in the prompt.
        if key != "seed" and key not in data:
            continue
        if isinstance(settings[key], list):
            for k in settings[key]:
                set_workflow_value(prompt_workflow, k.split("."), key, data)
        else:
            set_workflow_value(prompt_workflow, value.split('.'), key, data)
    return prompt_workflow


def _auto_tag_ref_prompt(data):
    """Prepend ``<Picture N>`` tags to the positive prompt for supplied images.

    Only meaningful for MiniMaxH3-style reference nodes that resolve
    ``<Picture N>`` tokens positionally. Called only when the workflow config
    opts in via ``"auto_image_tags": true``.
    """
    if "positive" not in data:
        return
    keys = sorted(
        (k for k in data if k.startswith("image") and k[5:].isdigit() and data[k]),
        key=lambda k: int(k[5:]),
    )
    if not keys:
        return
    tags = " ".join(f"<Picture {int(k[5:])}>" for k in keys)
    data["positive"] = tags + "\n\n" + data["positive"]


def _strip_optional_unset_nodes(api_prompt, fields, inputs_cfg, data):
    """Drop consumer slots and source nodes for optional fields that are unset.

    Only activates on inputs flagged ``optional: true`` in workflows.config.json,
    so existing untagged fields keep their previous behaviour (LoadImage widget
    falls back to its baked-in default filename when no file is supplied).

    Cascading removal: starting from each strippable source, walk down the
    chain of consumers. For each consumer:
      1. Disconnect the slot referencing its parent in the chain.
      2. If the consumer has no remaining list-typed inputs (i.e. nothing
         else feeds it), it has become orphaned — mark it for removal and
         continue walking down through its own consumers.
      3. Otherwise stop at this branch — the consumer is "real" (e.g. a
         main processing node with other inputs), we just severed the one
         slot that connected it to the chain.

    Safety: a source node is only stripped when **every** field referencing
    it is an unset optional. If any required+provided field also targets that
    source, we leave the source alone (its required field still needs it).
    This avoids accidentally severing a slot a required field depends on.

    Mutates ``api_prompt`` in place. Safe to call when nothing is optional
    (returns immediately).
    """
    fields = fields or {}
    inputs_cfg = inputs_cfg or {}

    unset_field_names = set()
    unset_field_sources = set()
    for fname, path in fields.items():
        spec = inputs_cfg.get(fname)
        if not spec or not spec.get("optional"):
            continue
        if fname in data and data[fname]:
            continue
        parts = (path or "").split(".")
        if len(parts) == 3 and parts[1] == "inputs":
            unset_field_names.add(fname)
            unset_field_sources.add(parts[0])

    if not unset_field_sources:
        return

    # Sources referenced by any required+provided field are protected — never
    # strip them, since the required field still needs the node to execute.
    protected_sources = set()
    for fname, path in fields.items():
        if fname in unset_field_names:
            continue
        if fname in data and data[fname]:
            parts = (path or "").split(".")
            if len(parts) == 3 and parts[1] == "inputs":
                protected_sources.add(parts[0])

    strippable_sources = unset_field_sources - protected_sources
    if not strippable_sources:
        return

    def find_consumers(src):
        """Return [(consumer_node_id, slot_name)] of every slot referencing src."""
        out = []
        for nid, node in api_prompt.items():
            if not isinstance(node, dict):
                continue
            for slot, value in (node.get("inputs") or {}).items():
                if (
                    isinstance(value, list)
                    and len(value) == 2
                    and isinstance(value[0], str)
                    and value[0] == src
                ):
                    out.append((nid, slot))
        return out

    def is_orphaned(nid):
        """A node is orphaned if it has no list-typed inputs (no connections)."""
        node = api_prompt.get(nid)
        if not isinstance(node, dict):
            return True
        for value in (node.get("inputs") or {}).values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
            ):
                return False
        return True

    to_remove = set(strippable_sources)

    # BFS from each strippable source through the consumer chain.
    for src in strippable_sources:
        queue = [src]
        visited = set()

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            for consumer_id, slot in find_consumers(current):
                # Disconnect the slot from this consumer.
                consumer_inputs = api_prompt.get(consumer_id, {}).get("inputs") or {}
                consumer_inputs.pop(slot, None)

                # If the consumer is now orphaned (no remaining list-typed
                # inputs), remove it and cascade through its own consumers.
                if consumer_id not in to_remove and is_orphaned(consumer_id):
                    to_remove.add(consumer_id)
                    queue.append(consumer_id)
                # If not orphaned, this consumer is "real" — leave it in place.

    for nid in to_remove:
        api_prompt.pop(nid, None)


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
    if workflow_config.get("auto_image_tags"):
        _auto_tag_ref_prompt(data)
    with open(workflow_config['prompt_workflow'], encoding='utf-8') as f:
        prompt_workflow = json.load(f)
    prompt_workflow = convert_ui_workflow_to_api(prompt_workflow)
    prompt_workflow = apply_settings(prompt_workflow, workflow_config['fields'], data)
    # Strip AFTER apply_settings so any leftover empty node entries (e.g. created
    # by setdefault walking into unset paths) are also removed. The cascade
    # also walks downstream consumers that have become orphaned.
    _strip_optional_unset_nodes(
        prompt_workflow,
        workflow_config.get('fields') or {},
        workflow_config.get('inputs') or {},
        data,
    )
    return queue_prompt(prompt_workflow)
