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


def _expand_subgraphs(workflow):
    """Expand subgraph parent nodes into their internal nodes with prefixed IDs.

    Iteratively expands any node whose ``type`` is a UUID found in
    ``definitions.subgraphs``. This handles recursive (nested) subgraphs:
    after an outer subgraph is expanded, its cloned internal nodes may
    themselves be UUID-typed subgraph parents, and those get expanded in the
    next pass. The loop terminates when no UUID-typed nodes remain or a safety
    cap is hit.

    For each parent, internal nodes get IDs of the form
    ``"<parent_id>:<original_id>"`` (e.g. ``105:10``; recursively,
    ``105:107:10``). External connections (via the implicit ``inputNode`` /
    ``outputNode`` IDs) are rewired to the surrounding graph; widget values
    stored on the parent are forwarded to the subgraph widget slots.

    Returns a new workflow dict with subgraphs flattened.
    """
    subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
    if not subgraphs:
        return workflow
    subgraph_by_id = {sg.get("id"): sg for sg in subgraphs}

    working_nodes = list(workflow.get("nodes") or [])
    working_links = list(workflow.get("links") or [])
    # Maps old link ID -> new link ID (for rewriting surviving nodes'
    # inputs[].link references). Persisted across iterations.
    link_id_remap = {}
    # Set of node IDs that have been consumed by expansion (already replaced
    # by their internal nodes). Persisted across iterations.
    expanded_parent_ids = set()

    # Safety cap: a sane upper bound on expansion depth to prevent infinite
    # loops if a workflow contains cyclic subgraph definitions.
    max_iterations = 32

    for _ in range(max_iterations):
        # Find any remaining node whose type is a subgraph UUID
        subgraph_parents = [
            n for n in working_nodes
            if n.get("type") in subgraph_by_id and n.get("id") not in expanded_parent_ids
        ]
        if not subgraph_parents:
            break

        next_link_id = max(
            (l[0] for l in working_links if isinstance(l, list) and l),
            default=0,
        ) + 1

        new_nodes = []
        new_links = []

        for parent in subgraph_parents:
            parent_type = parent.get("type")
            sg = subgraph_by_id.get(parent_type) if parent_type else None
            if sg is None:
                new_nodes.append(parent)
                continue

            parent_id = parent.get("id")
            expanded_parent_ids.add(parent_id)
            prefix = f"{parent_id}:"
            input_node_id = (sg.get("inputNode") or {}).get("id", -10)
            output_node_id = (sg.get("outputNode") or {}).get("id", -20)

            sg_inputs = sg.get("inputs") or []
            sg_outputs = sg.get("outputs") or []

            parent_inputs_list = parent.get("inputs") or []

            # For each parent input slot, find the working-graph link that sources it
            external_at_parent_slot = {}
            for pi_slot, pi in enumerate(parent_inputs_list):
                for tl in working_links:
                    if tl[3] == parent_id and tl[4] == pi_slot:
                        external_at_parent_slot[pi_slot] = (tl[1], tl[2])
                        break

            # Map each subgraph input slot -> (internal_link_id, external source or None, old_top_link_id)
            sg_in_external = []
            for sg_in_slot, sin in enumerate(sg_inputs):
                name = sin.get("name")
                pi_slot = None
                for pi_idx, pi in enumerate(parent_inputs_list):
                    if pi.get("name") == name:
                        pi_slot = pi_idx
                        break
                ext = external_at_parent_slot.get(pi_slot) if pi_slot is not None else None
                old_top_link_id = None
                for tl in working_links:
                    if tl[3] == parent_id and tl[4] == pi_slot:
                        old_top_link_id = tl[0]
                        break
                for internal_lid in (sin.get("linkIds") or []):
                    sg_in_external.append((internal_lid, ext, old_top_link_id))

            # Map each subgraph output slot -> list of (internal_link_id, dst_node, dst_slot, old_top_link_id)
            sg_out_external = []
            for sg_out_slot, sout in enumerate(sg_outputs):
                internal_lids = sout.get("linkIds") or []
                tl_dests = [tl for tl in working_links if tl[1] == parent_id and tl[2] == sg_out_slot]
                for internal_lid in internal_lids:
                    for tl in tl_dests:
                        sg_out_external.append((internal_lid, tl[3], tl[4], tl[0]))

            # Forward widget values: parent's widgets_values is positional for
            # widget-backed subgraph input slots, in subgraph.inputs order.
            forwarded_widgets = {}
            parent_wv = parent.get("widgets_values")
            if isinstance(parent_wv, list):
                wv_idx = 0
                for sin in sg_inputs:
                    name = sin.get("name")
                    parent_input = next((pi for pi in parent_inputs_list if pi.get("name") == name), None)
                    if parent_input is not None and "widget" not in parent_input:
                        continue
                    if wv_idx >= len(parent_wv):
                        break
                    value = parent_wv[wv_idx]
                    wv_idx += 1
                    for internal_lid in (sin.get("linkIds") or []):
                        forwarded_widgets[internal_lid] = value

            # Build new internal links with rewritten IDs
            link_remap = {}
            for sl in sg.get("links") or []:
                sl_id = sl["id"]
                if sl.get("origin_id") == input_node_id:
                    entry = next(((lid, e, oid) for lid, e, oid in sg_in_external if lid == sl_id), None)
                    if entry is None or entry[1] is None:
                        continue
                    ext_src, old_top = entry[1], entry[2]
                    origin = (str(ext_src[0]), ext_src[1])
                else:
                    origin = (prefix + str(sl["origin_id"]), sl["origin_slot"])

                if sl.get("target_id") == output_node_id:
                    dests = [(d_n, d_s, oid) for lid, d_n, d_s, oid in sg_out_external if lid == sl_id]
                    if not dests:
                        continue
                    new_link_id = next_link_id
                    next_link_id += 1
                    link_remap[sl_id] = new_link_id
                    for dst_node, dst_slot, old_top in dests:
                        new_links.append([
                            new_link_id, origin[0], origin[1], str(dst_node), dst_slot, sl.get("type", "*")
                        ])
                        if old_top is not None:
                            link_id_remap[old_top] = new_link_id
                    continue

                target = (prefix + str(sl["target_id"]), sl["target_slot"])
                new_link_id = next_link_id
                next_link_id += 1
                link_remap[sl_id] = new_link_id
                new_links.append([
                    new_link_id, origin[0], origin[1], target[0], target[1], sl.get("type", "*")
                ])
                if sl.get("origin_id") == input_node_id:
                    entry = next(((lid, e, oid) for lid, e, oid in sg_in_external if lid == sl_id), None)
                    if entry and entry[2] is not None:
                        link_id_remap[entry[2]] = new_link_id

            # Clone internal nodes with prefixed IDs
            for n in sg.get("nodes") or []:
                if str(n.get("id")) in (str(input_node_id), str(output_node_id)):
                    continue
                clone = json.loads(json.dumps(n))
                clone["id"] = prefix + str(clone["id"])
                for inp in clone.get("inputs") or []:
                    lid = inp.get("link")
                    if lid is not None and lid in link_remap:
                        inp["link"] = link_remap[lid]
                    elif lid is not None and lid in forwarded_widgets:
                        inp["link"] = None
                        clone.setdefault("_forwarded_widgets", {})[inp.get("name")] = forwarded_widgets[lid]
                    else:
                        inp["link"] = None
                new_nodes.append(clone)

        # Consolidate for next iteration: keep non-subgraph-parent nodes from
        # the previous pass, replace expanded parents with their cloned internals,
        # and merge in newly created links.
        next_working_nodes = []
        for n in working_nodes:
            if n.get("id") in expanded_parent_ids:
                # Skip — replaced by clones in new_nodes
                continue
            # Also rewrite any inputs[].link that was remapped this iteration
            for inp in n.get("inputs") or []:
                lid = inp.get("link")
                if lid is not None and lid in link_id_remap:
                    inp["link"] = link_id_remap[lid]
            next_working_nodes.append(n)
        next_working_nodes.extend(new_nodes)

        next_working_links = [
            l for l in working_links
            if isinstance(l, list) and len(l) >= 5 and l[1] not in expanded_parent_ids and l[3] not in expanded_parent_ids
        ]
        next_working_links.extend(new_links)

        working_nodes = next_working_nodes
        working_links = next_working_links

    expanded = dict(workflow)
    expanded["nodes"] = working_nodes
    expanded["links"] = working_links
    return expanded


def convert_ui_workflow_to_api(workflow):
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
        return workflow

    workflow = _expand_subgraphs(workflow)

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
        # Inputs that appear in UI but aren't widget-backed (e.g. optional
        # match-type slots with no widget value of their own). These should
        # not consume a widgets_values slot when iterating known_names.
        unbacked_optional_names = {
            ni["name"]
            for ni in node_inputs_list
            if ni.get("link") is None and not ni.get("widget") and ni.get("name")
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
            # Preserve connected inputs not in known_names (legacy schema drift)
            for name, value in connected.items():
                if name not in inputs:
                    inputs[name] = value
            converted[node_id] = {
                "inputs": inputs,
                "class_type": node_type,
                "_meta": {"title": node.get("title", node_type)},
            }
            continue

        widgets = [v for _, v in widget_pairs]
        forwarded = node.get("_forwarded_widgets") or {}
        for name in known_names:
            if name in connected:
                inputs[name] = connected[name]
                if name in widgeted_names and widgets:
                    widgets.pop(0)
                continue
            if name in forwarded:
                inputs[name] = forwarded[name]
                if name in widgeted_names and widgets:
                    widgets.pop(0)
                continue
            if name in unbacked_optional_names:
                continue
            if not widgets:
                continue
            inputs[name] = widgets.pop(0)
            if name == "seed" and widgets and widgets[0] in _CONTROL_AFTER_GENERATE_VALUES:
                widgets.pop(0)

        # Preserve connected inputs whose names aren't in the current backend
        # schema (e.g. legacy inputs the current /object_info no longer reports).
        for name, value in connected.items():
            if name not in inputs:
                inputs[name] = value

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
