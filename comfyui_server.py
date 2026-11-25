import base64
import json
import os
import time
from urllib.parse import quote, unquote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

import websocket

try:
    import markdown as markdown_lib
except ImportError:
    markdown_lib = None

from comfyui_config import (
    COMFYUI_PATH,
    comfyui_output_dir,
    local_path,
    mode_config_path,
)
from comfyui_images import (
    find_existing_file,
    is_valid_base64_image,
    save_base64_image,
    save_input_file,
)
import comfyui_jobs as jobs
import comfyui_process
import comfyui_state as st
from comfyui_process import start_comfyui, stop_comfyui
from comfyui_converter import convert_ui_workflow_to_api  # noqa: F401  (re-exported for callers)
from comfyui_workflow import (
    WorkflowError,
    comfyui_workflow,  # noqa: F401  (re-exported for callers)
    get_workflow_config,
    load_workflow_config,
)
from comfyui_websocket import open_websocket_connection, stop_websocket_connection

app = FastAPI()
app.state.max_request_size = 50 * 1024 * 1024  # 50MB limit
templates = Jinja2Templates(directory="templates")

AGENT_GUIDE_PATH = os.path.join(local_path, "docs", "AGENT_API_GUIDE.md")

# How long /comfyui_caption waits for the caption workflow before giving up.
CAPTION_TIMEOUT = 900.0

# File extensions that count as "text output" of a workflow.
TEXT_EXTENSIONS = ("txt", "text", "md", "json", "csv")

# Landing-page sections. Also serves as the description source for
# GET /api/endpoints, which merges these notes onto the live route table.
INDEX_GROUPS = [
    {
        'title': 'Start here',
        'entries': [
            {'method': 'GET', 'url': '/', 'path': '/',
             'description': 'This page. Every entry point, grouped by purpose.'},
            {'method': 'GET', 'url': '/agent_guide', 'path': '/agent_guide',
             'description': 'This integration guide, rendered as HTML. Read before your first job.'},
            {'method': 'GET', 'url': '/agent_guide.md', 'path': '/agent_guide.md',
             'description': 'The same guide as raw Markdown, for feeding straight to an agent.'},
            {'method': 'GET', 'url': '/api/workflows', 'path': '/api/workflows',
             'description': 'JSON map of workflow name to accepted payload keys and result method.'},
            {'method': 'GET', 'url': '/api/endpoints', 'path': '/api/endpoints',
             'description': 'JSON inventory of every route on this server.'},
            {'method': 'GET', 'url': '/mode_config', 'path': '/mode_config',
             'description': 'Merged high-level presets. Resolve the $placeholders in fields yourself.'},
            {'method': 'GET', 'url': '/openapi.json', 'path': '/openapi.json',
             'description': 'FastAPI-generated OpenAPI schema.'},
            {'method': 'GET', 'url': '/docs', 'path': '/docs',
             'description': 'Swagger UI for the generated schema.'},
        ],
    },
    {
        'title': 'Running a workflow',
        'entries': [
            {'method': 'POST', 'url': '/comfyui_workflow', 'path': '/comfyui_workflow',
             'description': 'Primary programmatic entry point. JSON body; image fields take raw base64 '
                            '(no data: prefix). Returns a job_id. Jobs are queued and run one at a time.'},
            {'method': 'POST', 'url': '/comfyui_runflow', 'path': '/comfyui_runflow',
             'description': 'Multipart entry point. Required for video/audio inputs. Only works for '
                            'workflows declaring an inputs block. Returns an HTML progress page, or JSON '
                            'with a job_id when the request sends Accept: application/json.'},
            {'method': 'POST', 'url': '/comfyui_caption', 'path': '/comfyui_caption',
             'description': 'Runs a caption workflow and blocks until the caption is written '
                            '(bounded by a 900 s timeout).'},
            {'method': 'GET', 'url': '/comfyui_text', 'path': '/comfyui_text',
             'description': 'The text output of one job (captions, tags, generated prompts). '
                            'Pass ?job_id= to address a specific job.'},
        ],
    },
    {
        'title': 'Status and control',
        'entries': [
            {'method': 'GET', 'url': '/comfyui_status', 'path': '/comfyui_status',
             'description': 'Poll this, ideally with ?job_id=. The job is finished when state is done, '
                            'error or cancelled (progress is then "Done"). Without a job_id the single '
                            'active job is served; with several active jobs the call fails with 409.'},
            {'method': 'POST', 'url': '/comfyui_cancel/{job_id}', 'path': '/comfyui_cancel/{job_id}',
             'description': 'Cancels exactly one job - the running one is interrupted, a queued one is '
                            'dequeued. Never touches another job.'},
            {'method': 'GET', 'url': '/comfyui_interrupt', 'path': '/comfyui_interrupt',
             'description': 'Alias for /comfyui_cancel/{job_id}. Requires ?job_id=; without it the call '
                            'fails with 400.'},
            {'method': 'POST', 'url': '/comfyui_interrupt_all', 'path': '/comfyui_interrupt_all',
             'description': 'Kills whatever ComfyUI is running, whoever submitted it - including jobs '
                            'started from the ComfyUI web UI. Use only when that is intended.'},
            {'method': 'GET', 'url': '/api/jobs', 'path': '/api/jobs',
             'description': 'Every job the gateway knows about, with their state and queue position.'},
            {'method': 'GET', 'url': '/api/jobs/{job_id}', 'path': '/api/jobs/{job_id}',
             'description': 'One job: state, progress, queue position and resolved output files.'},
            {'method': 'GET', 'url': '/start_comfyui', 'path': '/start_comfyui',
             'description': 'Start the ComfyUI backend. Idempotent; POST /comfyui_workflow returns 503 without it.'},
            {'method': 'GET', 'url': '/stop_comfyui', 'path': '/stop_comfyui',
             'description': 'Stop the ComfyUI backend.'},
            {'method': 'GET', 'url': '/comfyui_free', 'path': '/comfyui_free',
             'description': 'Unload models and free VRAM.'},
        ],
    },
    {
        'title': 'Results',
        'entries': [
            {'method': 'GET', 'url': '/find_workflow_outputs/{workflow}', 'path': '/find_workflow_outputs/{workflow}',
             'description': 'Use when /api/workflows shows has_output_config true. Requires the exact '
                            '<name>_api key — no suffix fallback. Pass ?job_id= to scope the listing to one '
                            'job. Build download URLs from "filepath"; the returned "file_url" is not routable.'},
            {'method': 'GET', 'url': '/comfyui_result', 'path': '/comfyui_result',
             'description': 'Base64 images under key "processed_image" (not "images"). Pass ?job_id= to '
                            'scope the result to one job. Only finds api*.jpg in the output root, so it '
                            'returns [] for most workflows.'},
            {'method': 'GET', 'url': '/download_file?filepath=', 'path': '/download_file?filepath=',
             'key': '/download_file',
             'description': 'Stream a file. Pass an absolute path exactly as the server returned it.'},
            {'method': 'GET', 'url': '/find_latest_runflow', 'path': '/find_latest_runflow',
             'description': 'Newest runflow* file in the output directory. Pass ?job_id= to restrict it '
                            'to one job.'},
            {'method': 'GET', 'url': '/loras', 'path': '/loras',
             'description': 'Available LoRA model paths, relative to the ComfyUI loras directory.'},
        ],
    },
    {
        'title': 'Web interface',
        'entries': [
            {'method': 'GET', 'url': '/runflow', 'path': '/runflow',
             'description': 'Workflow browser. Lists only workflows that declare an inputs block.'},
            {'method': 'GET', 'url': '/runflow/{path}', 'path': '/runflow/{path}',
             'key': '/runflow/{path:path}',
             'description': 'Same browser, scoped to a workflows/ subdirectory.'},
            {'method': 'GET', 'url': '/workflow/{workflow}', 'path': '/workflow/{workflow}',
             'description': 'Generated HTML form for one workflow. Takes the bare name, without _api.'},
            {'method': 'GET', 'url': '/workflow_status', 'path': '/workflow_status',
             'description': 'Standalone progress page. Pass ?job_id= to follow one job; without it the '
                            'page follows the single active job.'},
        ],
    },
]


# ---------------------------------------------------------------------------
# Discovery endpoints: index, agent guide, and machine-readable inventories.
# These let a remote agent learn the API surface without reading the source.
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page linking every entry point."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "base_url": str(request.base_url).rstrip('/'),
            "comfyui_running": bool(comfyui_process.comfyui_process),
            "groups": INDEX_GROUPS,
        }
    )


@app.get("/agent_guide.md")
async def agent_guide_markdown():
    """Agent integration guide as raw Markdown."""
    if not os.path.exists(AGENT_GUIDE_PATH):
        raise HTTPException(status_code=404, detail="Agent guide not found on disk")
    with open(AGENT_GUIDE_PATH, 'r', encoding='utf-8') as f:
        return PlainTextResponse(f.read(), media_type="text/markdown; charset=utf-8")


@app.get("/agent_guide", response_class=HTMLResponse)
async def agent_guide(request: Request):
    """Agent integration guide rendered as HTML (falls back to source if markdown is absent)."""
    if not os.path.exists(AGENT_GUIDE_PATH):
        raise HTTPException(status_code=404, detail="Agent guide not found on disk")
    with open(AGENT_GUIDE_PATH, 'r', encoding='utf-8') as f:
        markdown_text = f.read()

    html_body = None
    if markdown_lib is not None:
        html_body = markdown_lib.markdown(
            markdown_text, extensions=["tables", "fenced_code", "toc", "sane_lists"]
        )

    return templates.TemplateResponse(
        "agent_guide.html",
        {
            "request": request,
            "markdown_text": markdown_text,
            "html_body": html_body,
            "fallback": html_body is None,
        }
    )


@app.get('/api/workflows')
def api_workflows():
    """Every workflow with the payload keys it accepts and how to fetch its results.

    ``fields`` is the authoritative list of accepted payload keys for
    POST /comfyui_workflow. ``has_inputs`` marks workflows that can be driven
    from the web form / multipart endpoint. ``has_output_config`` marks
    workflows whose results are retrievable via /find_workflow_outputs;
    without it, fall back to /comfyui_result (api*.jpg in the output root).
    """
    out = {}
    for name, cfg in get_workflow_config(None, show_sub=True).items():
        prompt_workflow = cfg.get('prompt_workflow') or ''
        if prompt_workflow and not os.path.isabs(prompt_workflow):
            prompt_workflow = os.path.normpath(os.path.join(local_path, prompt_workflow))

        output_cfg = cfg.get('output') or {}
        produces_text = any(
            (spec.get('extension') or '').lower() in TEXT_EXTENSIONS
            for spec in (output_cfg.get('files') or [])
        )

        out[name] = {
            'prompt_workflow': prompt_workflow,
            'folder': os.path.basename(os.path.dirname(prompt_workflow)) if prompt_workflow else None,
            'fields': cfg.get('fields') or {},
            'inputs': cfg.get('inputs') or {},
            'auto_image_tags': bool(cfg.get('auto_image_tags')),
            'descriptions': cfg.get('descriptions'),
            'output': output_cfg,
            'produces_text': produces_text,
            'has_inputs': 'inputs' in cfg,
            'has_output_config': 'files' in output_cfg,
        }
    return out


@app.get('/api/endpoints')
def api_endpoints():
    """Route inventory, merged with notes on the non-obvious ones."""
    known = {}
    for group in INDEX_GROUPS:
        for entry in group['entries']:
            known[entry.get('key') or entry['path']] = entry

    out = []
    for route in app.routes:
        path = getattr(route, 'path', None)
        methods = sorted(m for m in (getattr(route, 'methods', None) or set()) if m != 'HEAD')
        if not path or not methods:
            continue
        note = known.get(path)
        out.append({
            'method': ','.join(methods),
            'path': path,
            # FastAPI copies the endpoint docstring onto route.description;
            # route.__doc__ is the route class docstring and is always None.
            'summary': getattr(route, 'description', None),
            'description': note['description'] if note else None,
        })
    return {'endpoints': out}


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
    jobs.stop_reconciler()
    stop_websocket_connection()
    return stop_comfyui()


# ComfyUI Status Endpoints
def _resolve_job(job_id):
    """Return a known job, or fail with 404."""
    job = jobs.registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return job


def _resolve_job_for_request(job_id):
    """Resolve the job a request is about.

    With a job_id the job must exist. Without one the single active job is
    served; anything else is ambiguous and fails loudly with 409 instead of
    silently returning another job's data.
    """
    if job_id:
        return _resolve_job(job_id)

    active = jobs.registry.active()
    if len(active) == 1:
        return active[0]
    if not active:
        raise HTTPException(
            status_code=409,
            detail="No active job. Pass job_id to address a specific job.",
        )
    raise HTTPException(
        status_code=409,
        detail=f"job_id is required: {len(active)} jobs are active. "
               "Poll /api/jobs to find the one you mean.",
    )


@app.get('/comfyui_status')
def comfyui_status(job_id: str = None):
    """Progress of one job.

    Pass ?job_id= to address a specific job. Without it the single active job
    is served, and a request that could match several jobs fails with 409.
    """
    job = _resolve_job_for_request(job_id)
    return job.status()


@app.get('/comfyui_interrupt')
def comfyui_interrupt(job_id: str = None):
    """Cancel one job. Alias for POST /comfyui_cancel/{job_id}."""
    if not job_id:
        raise HTTPException(
            status_code=400,
            detail="job_id is required. Use POST /comfyui_cancel/{job_id}. To stop whatever ComfyUI "
                   "is running regardless of owner use POST /comfyui_interrupt_all.",
        )
    return _cancel_job(job_id)


@app.post('/comfyui_cancel/{job_id}')
def comfyui_cancel(job_id: str):
    """Cancel exactly one job - running jobs are interrupted, queued jobs dequeued."""
    return _cancel_job(job_id)


def _cancel_job(job_id):
    result = jobs.cancel_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    return result


@app.post('/comfyui_interrupt_all')
def comfyui_interrupt_all():
    """Kill whatever ComfyUI is running - including jobs it did not submit."""
    return jobs.interrupt_all()


@app.get('/comfyui_free')
def comfyui_free():
    import requests
    requests.post("http://127.0.0.1:8188/free", json={"unload_models": True, "free_memory": True})
    return {"message": "ComfyUI memory released."}


@app.get('/comfyui_result')
def comfyui_result(job_id: str = None):
    """The results of one job."""
    job = _resolve_job_for_request(job_id)
    return {
        "job_id": job.job_id,
        "state": job.state,
        "processed_image": jobs.resolve_processed_images(job),
        "files": jobs.resolve_outputs(job),
    }


@app.get('/comfyui_text')
def comfyui_text(job_id: str = None):
    """The text output of one job.

    Workflows that write a text file (captions, tags, generated prompts) expose
    their result here, so a client can read it without downloading the file.
    The text is taken from the job's own output files, so two jobs writing
    ``caption.txt`` at the same time never mix their results up.
    """
    job = _resolve_job_for_request(job_id)
    text, error = _read_job_text(job)
    return {
        "job_id": job.job_id,
        "state": job.state,
        "text": text or "",
        "error": job.error or error,
        "files": jobs.resolve_outputs(job),
    }


def _job_text_files(job):
    """The text files a job produced."""
    files = jobs.resolve_outputs(job)
    return [f for f in files if (f.get("extension") or "").lower() in TEXT_EXTENSIONS]


def _read_job_text(job):
    """Read the text output of a job.

    Returns ``(text, error)``. ``text`` is None when the job has no text file
    (yet); ``error`` describes why a file could not be read.
    """
    for entry in _job_text_files(job):
        try:
            with open(entry["filepath"], "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(), None
        except OSError as exc:
            return None, f"Cannot read {entry['filepath']}: {exc}"
    return None, None


@app.get('/api/jobs')
def api_jobs(client_id: str = None, state: str = None):
    """Every job the gateway knows about."""
    return {
        "jobs": [job.status() for job in jobs.registry.all(client_id=client_id, state=state)]
    }


@app.get('/api/jobs/{job_id}')
def api_job(job_id: str):
    """One job, with its resolved output files."""
    job = _resolve_job(job_id)
    payload = job.status()
    payload["outputs"] = jobs.resolve_outputs(job)
    return payload


@app.get('/find_latest_runflow')
def find_latest_runflow_file(job_id: str = None):
    """Find the latest file starting with 'runflow' in the comfyui_output_dir"""
    try:
        runflow_files = []
        job = jobs.registry.get(job_id) if job_id else None
        if job_id and job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")

        window_start, window_end = job.window() if job else (None, None)
        history_names = {out["filename"] for out in job.outputs} if job else set()

        if os.path.exists(comfyui_output_dir):
            for filename in os.listdir(comfyui_output_dir):
                if filename.startswith("runflow"):
                    filepath = os.path.join(comfyui_output_dir, filename)
                    if os.path.isfile(filepath):
                        mod_time = os.path.getmtime(filepath)
                        if job is not None:
                            in_window = window_start <= mod_time <= window_end
                            if not in_window and filename not in history_names:
                                continue
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

    except HTTPException:
        raise
    except Exception as e:
        return {'file_url': None, 'filename': None, 'error': str(e)}


@app.get('/find_workflow_outputs/{workflow}')
def find_workflow_outputs(workflow: str, job_id: str = None):
    """Find all output files for a workflow based on output config"""
    try:
        config = load_workflow_config(workflow)
        output_config = config.get('output', {})

        if 'files' not in output_config:
            return {'files': [], 'has_output_config': False, 'debug': {
                'workflow': workflow,
                'comfyui_output_dir': comfyui_output_dir
            }}

        job = None
        if job_id:
            job = jobs.registry.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
            jobs.ensure_history(job)
            window_start, window_end = job.window()
        else:
            active = jobs.registry.active()
            if len(active) == 1:
                window_start, window_end = active[0].window()
            else:
                window_start, window_end = 0.0, None

        file_specs = output_config.get('files', [])
        all_files = jobs.find_output_files(workflow, (window_start, window_end))

        return {
            'files': [{
                'file_url': f"/download_file?filepath={quote(f['filepath'])}",
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
                'job_id': job_id,
                'workflow_start_time': window_start,
                'file_specs': file_specs
            }
        }
    except HTTPException:
        raise
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

        if not os.path.isfile(full_path):
            # Starlette's FileResponse raises RuntimeError on a directory,
            # which surfaces as an unhandled ASGI exception (500).
            raise HTTPException(status_code=404, detail=f"Not a file: {full_path}")

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


def _workflow_fields(workflow_name):
    """The field map of a workflow, empty when it cannot be read."""
    try:
        return load_workflow_config(workflow_name).get('fields') or {}
    except Exception:
        return {}


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

    workflow_name = _resolve_workflow_name(data['workflow'])
    client_id = request.headers.get('X-Client-Id')

    # The job (and its ComfyUI prompt id) exists before the input files are
    # written, so every input file can be owned by exactly one job.
    job = jobs.create_job(workflow_name, client_id)

    try:
        for key, value in data.items():
            if isinstance(value, str) and is_valid_base64_image(value):
                image_bytes = base64.b64decode(value)
                # Input files are content-addressed: the same bytes always map
                # to the same file name, so ComfyUI reuses its cached node
                # output instead of decoding the image again.
                existing_path = find_existing_file(key, image_bytes)
                if existing_path:
                    data[key] = existing_path
                    job.input_files.append(existing_path)
                else:
                    saved_path = save_base64_image(value, key)
                    data[key] = saved_path
                    job.input_files.append(saved_path)

        jobs.submit_job(job, data)
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"comfyui_workflow failed: {e}")

    if not st.ws:
        open_websocket_connection()

    return {
        "status": "Queued",
        "job_id": job.job_id,
        "state": job.state,
        "workflow": workflow_name,
        "queue_position": job.queue_position,
        "queue_length": job.queue_length,
    }


@app.post('/comfyui_runflow')
async def comfyui_runflow_handler(request: Request):
    """Multipart entry point: the web form's target and the agent video/audio route.

    Every submission is its own job. The default response is the HTML progress
    page; a request that sends ``Accept: application/json`` gets the same job
    envelope as POST /comfyui_workflow instead.
    """
    form_data = await request.form()

    if not comfyui_process.comfyui_process:
        return {"message": "ComfyUI is not started."}

    workflow_name = form_data.get('workflow')
    if not workflow_name:
        raise HTTPException(status_code=400, detail="Workflow not specified")

    try:
        workflow_config = load_workflow_config(workflow_name)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_name}' not found in any workflows.config.json",
        )

    if "inputs" not in workflow_config:
        raise HTTPException(
            status_code=400,
            detail=f"Workflow '{workflow_name}' has no inputs block - use POST /comfyui_workflow",
        )

    client_id = request.headers.get('X-Client-Id')
    job = jobs.create_job(workflow_name, client_id)
    data = {}

    try:
        for field_name, field in workflow_config["inputs"].items():
            field_type = field["type"]

            if field_type in ["image", "video", "audio"]:
                file = form_data.get(field_name)
                if file and file.filename:
                    file_content = await file.read()
                    existing_path = find_existing_file(field_name, file_content)
                    if existing_path:
                        data[field_name] = existing_path
                        job.input_files.append(existing_path)
                    else:
                        file_ext = os.path.splitext(file.filename)[1]
                        save_path = save_input_file(file_content, field_name, extension=file_ext)
                        data[field_name] = save_path
                        job.input_files.append(save_path)
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

        jobs.submit_job(job, data)
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"comfyui_runflow failed: {e}")

    if not st.ws:
        open_websocket_connection()

    accepts_json = "application/json" in (request.headers.get("accept") or "").lower()
    if accepts_json:
        return {
            "status": "Queued",
            "job_id": job.job_id,
            "state": job.state,
            "workflow": workflow_name,
            "queue_position": job.queue_position,
            "queue_length": job.queue_length,
        }

    return templates.TemplateResponse(
        "workflow_progress.html",
        {
            "request": request,
            "workflow": workflow_name,
            "job_id": job.job_id,
            "start_time": job.submitted_at,
        }
    )


@app.post('/comfyui_caption')
async def comfyui_caption(request: Request):
    """Run a caption workflow and block until the caption is written.

    The caption is read from the job's own output files, so two caption jobs
    running at the same time cannot pick up each other's text.
    """
    data = await request.json()

    if not comfyui_process.comfyui_process:
        return {"caption": "", "error": "ComfyUI is not started."}

    if "background" not in data:
        return {"caption": "", "error": "Missing 'background' in request body"}

    workflow_name = _resolve_workflow_name(data['workflow'])
    client_id = request.headers.get('X-Client-Id')
    job = jobs.create_job(workflow_name, client_id)

    try:
        data["background"] = save_base64_image(data['background'], "background")
        job.input_files.append(data["background"])
        # The text workflows take their image as "image1"; this endpoint keeps
        # its old "background" name.
        if "image1" in _workflow_fields(workflow_name):
            data["image1"] = data["background"]
        jobs.submit_job(job, data)
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"comfyui_caption failed: {e}")

    if not st.ws:
        open_websocket_connection()

    deadline = time.time() + CAPTION_TIMEOUT
    while time.time() < deadline:
        if job.is_terminal:
            # The job is done, but the file may still be on its way to disk:
            # give it a moment before giving up.
            file_deadline = time.time() + 10.0
            read_error = None
            while time.time() < file_deadline:
                text, read_error = _read_job_text(job)
                if text is not None:
                    return {"caption": text, "job_id": job.job_id}
                if job.state == jobs.STATE_ERROR:
                    break
                time.sleep(0.5)
            return {
                "caption": "",
                "error": job.error or read_error or "caption workflow finished without producing a caption",
                "job_id": job.job_id,
            }
        time.sleep(0.5)

    return {"caption": "", "error": "timeout", "job_id": job.job_id}


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
async def workflow_status_page(request: Request, job_id: str = None):
    """Standalone progress page.

    With ?job_id= it follows that job. Without one it follows the single
    active job, if there is exactly one - it never shows another job's data.
    """
    job = None
    if job_id:
        job = jobs.registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job_id: {job_id}")
    else:
        active = jobs.registry.active()
        if len(active) == 1:
            job = active[0]

    return templates.TemplateResponse(
        "workflow_progress.html",
        {
            "request": request,
            "job_id": job.job_id if job else "",
            "workflow": job.workflow if job else "",
            "start_time": job.submitted_at if job else None,
        }
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
                "required": not field.get("optional", False),
                "optional": field.get("optional", False),
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
