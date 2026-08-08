"""Convert ComfyUI UI workflow JSON to API prompt JSON.

Handles:
- Subgraph expansion (including recursive/nested subgraphs) — internal node
  IDs become ``"<parent_id>:<original_id>"`` (e.g. ``105:10``).
- SetNode/GetNode variable propagation — bypasses these helper nodes by
  wiring the SetNode's source directly to each consumer.
- Dict-form and list-form ``widgets_values`` representations.
- ``control_after_generate`` paired values (``"randomize"``, ``"increment"``,
  ``"decrement"``, ``"fixed"``) — frontend-only state dropped after ``seed``.
- Disabled nodes (``mode == 4``) and MarkdownNote / Note nodes.
"""
import json
from urllib import request
from urllib.parse import quote

from comfyui_config import comfyui_server

_node_input_names_cache = {}


def get_node_input_names(node_type):
    """Fetch ordered list of input names for a node type from ComfyUI /object_info.

    Cached. Returns None on failure so callers can fall back gracefully.
    """
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


# --- Constants ---------------------------------------------------------------

_CONTROL_AFTER_GENERATE_VALUES = {"randomize", "increment", "decrement", "fixed"}
_SETGET_TYPES = {"SetNode", "GetNode"}
_SKIPPED_NODE_TYPES = {"MarkdownNote", "Note"}
# UI-only widget keys in dict-form widgets_values that must never reach the backend.
_UI_WIDGET_KEY_BLACKLIST = {
    "videopreview",
    "choose video to upload",
    "choose image to upload",
}


# --- Widget iteration --------------------------------------------------------

def _widget_iter(widgets_values):
    """Yield ``(name, value)`` pairs from a node's widgets_values.

    Supports list-form (positional) and dict-form (keyed) representations.
    """
    if isinstance(widgets_values, dict):
        for name, value in widgets_values.items():
            if name in _UI_WIDGET_KEY_BLACKLIST:
                continue
            yield name, value
        return
    for value in (widgets_values or []):
        yield None, value


def _skip_control_after_generate(widgets):
    """Drop a ``control_after_generate`` paired value if present at the head."""
    if widgets and widgets[0] in _CONTROL_AFTER_GENERATE_VALUES:
        widgets.pop(0)


# --- SetNode / GetNode propagation -------------------------------------------

def _build_setget_rewrites(nodes, links):
    """Compute the rewrite maps for SetNode/GetNode propagation.

    Returns ``(setget_ids, link_id_rewrite)``:
    - ``setget_ids``: node IDs to drop entirely.
    - ``link_id_rewrite``: map of ``{old_link_id: source_link_id}`` so consumers
      of a GetNode output (or a SetNode output that wasn't routed through a
      GetNode) can be rewired to the SetNode's input link.
    """
    name_to_source = {}
    setget_ids = set()
    for n in nodes:
        ntype = n.get("type")
        if ntype not in _SETGET_TYPES:
            continue
        setget_ids.add(n.get("id"))
        if ntype == "SetNode":
            wv = n.get("widgets_values")
            if not (isinstance(wv, list) and wv):
                continue
            name = wv[0]
            for inp in n.get("inputs") or []:
                if inp.get("link") is not None:
                    name_to_source[name] = inp["link"]
                    break
            else:
                name_to_source[name] = None

    rewrites = {}
    for n in nodes:
        ntype = n.get("type")
        if ntype not in _SETGET_TYPES:
            continue
        wv = n.get("widgets_values")
        if not (isinstance(wv, list) and wv):
            continue
        name = wv[0]
        source_link = name_to_source.get(name)
        if source_link is None:
            continue
        for out in n.get("outputs") or []:
            for lid in out.get("links") or []:
                rewrites[lid] = source_link
    return setget_ids, rewrites


def _propagate_setget(workflow):
    """Eliminate SetNode/GetNode pairs by inlining their variable value."""
    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []
    if not nodes:
        return workflow

    setget_ids, rewrites = _build_setget_rewrites(nodes, links)
    if not setget_ids and not rewrites:
        return workflow

    kept_nodes = [n for n in nodes if n.get("id") not in setget_ids]

    new_links = []
    for l in links:
        if not (isinstance(l, list) and len(l) >= 5):
            new_links.append(l)
            continue
        lid, src, src_slot, dst, dst_slot, ltype = l[:6]
        if src in setget_ids:
            if lid not in rewrites:
                continue
            src_link_id = rewrites[lid]
            for sl in links:
                if isinstance(sl, list) and sl and sl[0] == src_link_id:
                    src, src_slot = sl[1], sl[2]
                    break
        new_links.append([lid, src, src_slot, dst, dst_slot, ltype])

    for n in kept_nodes:
        for inp in n.get("inputs") or []:
            lid = inp.get("link")
            if lid is not None and lid in rewrites:
                inp["link"] = rewrites[lid]

    out = dict(workflow)
    out["nodes"] = kept_nodes
    out["links"] = new_links
    return out


# --- Subgraph expansion ------------------------------------------------------

def _external_at_parent_slot(parent_inputs, parent_id, working_links):
    """For each parent input slot, find the working-graph link that targets it."""
    result = {}
    for pi_slot, pi in enumerate(parent_inputs):
        for tl in working_links:
            if tl[3] == parent_id and tl[4] == pi_slot:
                result[pi_slot] = (tl[1], tl[2])
                break
    return result


def _build_subgraph_io_maps(sg, parent_inputs_list, parent_id, working_links):
    """Map subgraph input/output slots to (internal_link_id, external source/destination).

    Returns ``(sg_in_external, sg_out_external)`` where each is a list of
    tuples keyed by subgraph input/output slot index.
    """
    external_at = _external_at_parent_slot(parent_inputs_list, parent_id, working_links)
    sg_inputs = sg.get("inputs") or []
    sg_outputs = sg.get("outputs") or []

    sg_in_external = []
    for sin in sg_inputs:
        name = sin.get("name")
        pi_slot = next(
            (i for i, pi in enumerate(parent_inputs_list) if pi.get("name") == name),
            None,
        )
        ext = external_at.get(pi_slot) if pi_slot is not None else None
        old_top_link_id = next(
            (tl[0] for tl in working_links if tl[3] == parent_id and tl[4] == pi_slot),
            None,
        )
        for internal_lid in (sin.get("linkIds") or []):
            sg_in_external.append((internal_lid, ext, old_top_link_id))

    sg_out_external = []
    for sg_out_slot, sout in enumerate(sg_outputs):
        tl_dests = [tl for tl in working_links if tl[1] == parent_id and tl[2] == sg_out_slot]
        for internal_lid in (sout.get("linkIds") or []):
            for tl in tl_dests:
                sg_out_external.append((internal_lid, tl[3], tl[4], tl[0]))
    return sg_in_external, sg_out_external


def _forwarded_widgets_for_parent(parent, sg_inputs):
    """Build ``{internal_link_id: widget_value}`` from parent's widgets_values.

    The parent's widgets_values is positional for widget-backed subgraph input
    slots in order. Image-only inputs (no widget consumer) are skipped.
    """
    parent_wv = parent.get("widgets_values")
    if not isinstance(parent_wv, list):
        return {}

    parent_inputs_list = parent.get("inputs") or []
    forwarded = {}
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
            forwarded[internal_lid] = value
    return forwarded


def _expand_one_subgraph(sg, parent, working_links, next_link_id, link_id_remap):
    """Expand one parent node into its internal nodes.

    Returns ``(new_nodes, new_links, next_link_id)``.
    """
    parent_id = parent.get("id")
    prefix = f"{parent_id}:"
    input_node_id = (sg.get("inputNode") or {}).get("id", -10)
    output_node_id = (sg.get("outputNode") or {}).get("id", -20)
    sg_inputs = sg.get("inputs") or []

    sg_in_external, sg_out_external = _build_subgraph_io_maps(
        sg, parent.get("inputs") or [], parent_id, working_links,
    )
    forwarded_widgets = _forwarded_widgets_for_parent(parent, sg_inputs)

    link_remap = {}
    new_links = []
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
                new_links.append([new_link_id, origin[0], origin[1], str(dst_node), dst_slot, sl.get("type", "*")])
                if old_top is not None:
                    link_id_remap[old_top] = new_link_id
            continue

        target = (prefix + str(sl["target_id"]), sl["target_slot"])
        new_link_id = next_link_id
        next_link_id += 1
        link_remap[sl_id] = new_link_id
        new_links.append([new_link_id, origin[0], origin[1], target[0], target[1], sl.get("type", "*")])
        if sl.get("origin_id") == input_node_id:
            entry = next(((lid, e, oid) for lid, e, oid in sg_in_external if lid == sl_id), None)
            if entry and entry[2] is not None:
                link_id_remap[entry[2]] = new_link_id

    new_nodes = []
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
    return new_nodes, new_links, next_link_id


def _expand_subgraphs(workflow, max_iterations=32):
    """Iteratively expand subgraph parent nodes (handles recursive nesting)."""
    subgraphs = (workflow.get("definitions") or {}).get("subgraphs") or []
    if not subgraphs:
        return workflow
    subgraph_by_id = {sg.get("id"): sg for sg in subgraphs}

    working_nodes = list(workflow.get("nodes") or [])
    working_links = list(workflow.get("links") or [])
    link_id_remap = {}
    expanded_parent_ids = set()

    for _ in range(max_iterations):
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

        new_nodes, new_links, next_link_id = [], [], next_link_id
        for parent in subgraph_parents:
            sg = subgraph_by_id.get(parent["type"])
            if sg is None:
                new_nodes.append(parent)
                continue
            expanded_parent_ids.add(parent["id"])
            ns, ls, next_link_id = _expand_one_subgraph(
                sg, parent, working_links, next_link_id, link_id_remap,
            )
            new_nodes.extend(ns)
            new_links.extend(ls)

        next_working_nodes = []
        for n in working_nodes:
            if n.get("id") in expanded_parent_ids:
                continue
            for inp in n.get("inputs") or []:
                lid = inp.get("link")
                if lid is not None and lid in link_id_remap:
                    inp["link"] = link_id_remap[lid]
            next_working_nodes.append(n)
        next_working_nodes.extend(new_nodes)

        next_working_links = [
            l for l in working_links
            if isinstance(l, list) and len(l) >= 5
            and l[1] not in expanded_parent_ids and l[3] not in expanded_parent_ids
        ]
        next_working_links.extend(new_links)

        working_nodes = next_working_nodes
        working_links = next_working_links

    expanded = dict(workflow)
    expanded["nodes"] = working_nodes
    expanded["links"] = working_links
    return expanded


# --- Per-node conversion -----------------------------------------------------

def _node_input_groups(node_inputs):
    """Split a node's inputs[] into connected names and per-slot metadata.

    Returns ``(widgeted_names, unbacked_optional_names)``.
    """
    widgeted_names = {
        ni["widget"]["name"]
        for ni in node_inputs
        if ni.get("widget") and ni.get("widget", {}).get("name")
    }
    unbacked_optional_names = {
        ni["name"]
        for ni in node_inputs
        if ni.get("link") is None and not ni.get("widget") and ni.get("name")
    }
    return widgeted_names, unbacked_optional_names


def _connected_inputs(node_inputs, links):
    """Map input name -> [source_node, source_slot] for each connected input."""
    connected = {}
    for ni in node_inputs:
        name = ni.get("name")
        if not name:
            continue
        link_id = ni.get("link")
        if link_id is not None and link_id in links:
            source_node, source_slot = links[link_id]
            connected[name] = [str(source_node), source_slot]
    return connected


def _convert_list_branch(node, known_names, widgeted_names, unbacked_optional_names,
                         connected, widgets, forwarded):
    """Convert a node whose widgets_values is a positional list."""
    inputs = {}
    for name in known_names:
        if name in connected:
            inputs[name] = connected[name]
            if name in widgeted_names and widgets:
                widgets.pop(0)
                if name == "seed":
                    _skip_control_after_generate(widgets)
            continue
        if name in forwarded:
            inputs[name] = forwarded[name]
            if name in widgeted_names and widgets:
                widgets.pop(0)
                if name == "seed":
                    _skip_control_after_generate(widgets)
            continue
        if name in unbacked_optional_names:
            continue
        if not widgets:
            continue
        inputs[name] = widgets.pop(0)
        if name == "seed":
            _skip_control_after_generate(widgets)
    # Preserve connected inputs whose names aren't in known_names (legacy schema).
    for name, value in connected.items():
        if name not in inputs:
            inputs[name] = value
    return inputs


def _convert_dict_branch(node, connected):
    """Convert a node whose widgets_values is a dict (named widgets)."""
    inputs = {}
    for name, value in _widget_iter(node.get("widgets_values")):
        if name and value is not None:
            inputs[name] = value
    for name, value in connected.items():
        inputs[name] = value
    # Preserve connected inputs not in known_names (legacy schema drift).
    for name, value in connected.items():
        if name not in inputs:
            inputs[name] = value
    return inputs


def _convert_fallback_branch(connected, widget_pairs):
    """Last-resort: emit connected inputs and the rest as ``valueN`` placeholders."""
    inputs = dict(connected)
    for _, value in widget_pairs:
        inputs[f"value{len(inputs)}"] = value
    return inputs


def _convert_node(node, links):
    """Convert one UI workflow node to its API prompt entry."""
    node_type = node.get("type")
    node_id = str(node.get("id"))
    node_inputs_list = node.get("inputs") or []
    widgeted_names, unbacked_optional_names = _node_input_groups(node_inputs_list)
    connected = _connected_inputs(node_inputs_list, links)
    widget_pairs = list(_widget_iter(node.get("widgets_values")))
    widgets = [v for _, v in widget_pairs]
    forwarded = node.get("_forwarded_widgets") or {}

    known_names = get_node_input_names(node_type)
    if known_names is None:
        inputs = _convert_fallback_branch(connected, widget_pairs)
    elif isinstance(node.get("widgets_values"), dict):
        inputs = _convert_dict_branch(node, connected)
    else:
        inputs = _convert_list_branch(
            node, known_names, widgeted_names, unbacked_optional_names,
            connected, widgets, forwarded,
        )

    return node_id, {
        "inputs": inputs,
        "class_type": node_type,
        "_meta": {"title": node.get("title", node_type)},
    }


def convert_ui_workflow_to_api(workflow):
    """Convert a ComfyUI UI workflow JSON to its API prompt JSON form."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get("nodes"), list):
        return workflow

    workflow = _propagate_setget(workflow)
    workflow = _expand_subgraphs(workflow)

    links = {}
    for link in workflow.get("links", []):
        if isinstance(link, list) and len(link) >= 5:
            links[link[0]] = (link[1], link[2])

    converted = {}
    for node in workflow["nodes"]:
        if node.get("mode") == 4:
            continue
        if node.get("type") in _SKIPPED_NODE_TYPES:
            continue
        nid, api_node = _convert_node(node, links)
        converted[nid] = api_node
    return converted
