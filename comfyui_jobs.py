"""Per-job state for every workflow the gateway submits.

The gateway used to keep a single global "current job" slot, which meant that
two overlapping submissions fought over the same progress text and that an
interrupt killed whatever ComfyUI happened to be running. Every submission now
gets its own :class:`Job` record here, and the ComfyUI prompt id *is* the
gateway job id, so status and cancellation are plain delegations to ComfyUI's
own ``/api/jobs`` endpoints.
"""
import base64
import os
import threading
import time
import uuid

import requests

from comfyui_config import comfyui_output_dir, comfyui_server
from comfyui_images import INPUT_EXTENSIONS, delete_input_file, inputs_dir
from comfyui_workflow import build_prompt, load_workflow_config, queue_prompt

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"

ACTIVE_STATES = (STATE_QUEUED, STATE_RUNNING)
TERMINAL_STATES = (STATE_DONE, STATE_ERROR, STATE_CANCELLED)

COMFYUI_BASE = f"http://{comfyui_server}"
REQUEST_TIMEOUT = 15
RECONCILE_INTERVAL = 1.0
# A job that ComfyUI does not know about right after submission is usually
# just a race with the queue; only call it lost after this grace period.
LOST_GRACE_SECONDS = 5.0
# How often (in reconciler ticks) the input/output sweeps run.
SWEEP_EVERY_TICKS = 30
# Age after which unreferenced input and output files are removed.
ORPHAN_MAX_AGE = 3600.0
# The websocket "executing" message arrives a moment after the job actually
# started, so the estimated start time is backdated by this much when no
# authoritative execution start time is known yet.
WINDOW_TOLERANCE = 1.0


class Job:
    """One submitted workflow, with its own progress and result bookkeeping."""

    def __init__(self, job_id, workflow, client_id=None):
        self.job_id = job_id
        self.workflow = workflow
        self.client_id = client_id
        self.state = STATE_QUEUED
        self.progress = ""
        self.step = ""
        self.error = None
        self.submitted_at = time.time()
        self.started_at = None
        self.ended_at = None
        # The authoritative execution start reported by ComfyUI's history.
        self.authoritative_start = None
        # The authoritative execution end reported by ComfyUI's history.
        self.authoritative_end = None
        # Whether ComfyUI's history entry for this job has been fetched yet.
        self.history_fetched = False
        self.queue_position = None
        self.queue_length = 0
        self.outputs = []
        self.input_files = []
        # Per-job progress bookkeeping (the websocket handler needs the prompt
        # to resolve node titles and the node count for the progress text).
        self.prompt = None
        self.node_ids = []
        self.finished_nodes = []
        self.cur_node = ""
        self.last_node = ""
        self.last_time = 0.0
        self.last_step = 0

    @property
    def is_terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def is_active(self):
        return self.state in ACTIVE_STATES

    def update(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def window(self):
        """The (start, end) time window during which this job produced files."""
        if self.authoritative_start is not None:
            start = self.authoritative_start
        elif self.started_at is not None:
            # The websocket message arrives after the job started - backdate a
            # little so files written in the first moments are still included.
            start = self.started_at - WINDOW_TOLERANCE
        else:
            start = self.submitted_at

        # ComfyUI runs the jobs one after another, so the previous job's last
        # file is the upper bound for this job's first file. Using it keeps the
        # previous job's output out of this job's result list.
        previous_end = self._previous_job_end()
        if previous_end is not None and previous_end > start:
            start = previous_end

        end = self.ended_at if self.ended_at is not None else time.time() + 1.0
        return start, end

    def _previous_job_end(self):
        """The authoritative end time of the job that ran before this one."""
        previous_end = None
        for other in registry.all():
            if other is self or other.authoritative_end is None:
                continue
            if other.submitted_at >= self.submitted_at:
                continue
            if previous_end is None or other.authoritative_end > previous_end:
                previous_end = other.authoritative_end
        return previous_end

    def status(self):
        """The public status payload for this job."""
        if self.state == STATE_QUEUED:
            if self.queue_position:
                progress = f"Queued (position {self.queue_position} of {self.queue_length})"
            else:
                progress = "Queued"
        elif self.is_terminal:
            # Legacy clients stop polling when progress is exactly "Done".
            progress = "Done"
        else:
            progress = self.progress

        return {
            "job_id": self.job_id,
            "workflow": self.workflow,
            "client_id": self.client_id,
            "state": self.state,
            "progress": progress,
            "step": self.step,
            "error": self.error,
            "queue_position": self.queue_position,
            "queue_length": self.queue_length,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class JobRegistry:
    """Thread-safe store of :class:`Job` records."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}

    def add(self, job):
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id):
        if not job_id:
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def all(self, client_id=None, state=None):
        with self._lock:
            jobs = list(self._jobs.values())
        if client_id:
            jobs = [j for j in jobs if j.client_id == client_id]
        if state:
            wanted = {s.strip() for s in state.split(",") if s.strip()}
            jobs = [j for j in jobs if j.state in wanted]
        jobs.sort(key=lambda j: j.submitted_at)
        return jobs

    def active(self):
        with self._lock:
            return [j for j in self._jobs.values() if j.is_active]

    def remove(self, job_id):
        with self._lock:
            return self._jobs.pop(job_id, None)


registry = JobRegistry()


# ---------------------------------------------------------------------------
# ComfyUI helpers
# ---------------------------------------------------------------------------

def _comfy_get(path, timeout=REQUEST_TIMEOUT):
    """GET a ComfyUI JSON endpoint.

    Returns ``(reachable, payload)``. ``reachable`` is False when the request
    itself failed (ComfyUI down / timeout), in which case the payload is None.
    A 404 is a successful request with a missing resource: ``(True, None)``.
    """
    try:
        resp = requests.get(f"{COMFYUI_BASE}{path}", timeout=timeout)
    except requests.RequestException:
        return False, None

    if resp.status_code == 404:
        return True, None
    if not resp.ok:
        return False, None
    try:
        return True, resp.json()
    except ValueError:
        return False, None


def flatten_outputs(outputs):
    """Flatten ComfyUI history ``outputs`` into a list of file records."""
    flat = []
    if not isinstance(outputs, dict):
        return flat

    for node_id, node_outputs in outputs.items():
        if not isinstance(node_outputs, dict):
            continue
        for media_type, items in node_outputs.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                if not filename:
                    continue
                subfolder = item.get("subfolder") or ""
                flat.append({
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": item.get("type") or "output",
                    "media_type": media_type,
                    "node_id": node_id,
                    "filepath": os.path.join(comfyui_output_dir, subfolder, filename),
                })
    return flat


def _error_message(execution_error):
    if not isinstance(execution_error, dict):
        return None
    exception_type = execution_error.get("exception_type")
    message = execution_error.get("exception_message")
    if exception_type and message:
        return f"{exception_type}: {message}"
    return message or exception_type or None


def apply_history(job, detail):
    """Apply a ComfyUI ``/api/jobs/{id}`` history record to a job."""
    status = detail.get("status")
    if status == "completed":
        job.state = STATE_DONE
    elif status == "cancelled":
        job.state = STATE_CANCELLED
    elif status == "failed":
        job.state = STATE_ERROR
        job.error = _error_message(detail.get("execution_error"))
    else:
        return
    start = detail.get("execution_start_time")
    end = detail.get("execution_end_time")
    if start:
        job.authoritative_start = start / 1000.0
        job.started_at = job.authoritative_start
    if end:
        job.authoritative_end = end / 1000.0
        job.ended_at = job.authoritative_end
    outputs = flatten_outputs(detail.get("outputs") or {})
    if outputs:
        job.outputs = outputs
    job.progress = "Done"
    job.step = ""


def ensure_history(job):
    """Fetch ComfyUI's history entry for a job, once.

    The history entry carries the authoritative execution timestamps and the
    list of output files. It is fetched as soon as the job reaches a terminal
    state, and on demand whenever a client asks for the job's results.
    """
    if job is None or job.history_fetched:
        return

    found, detail = _comfy_get(f"/api/jobs/{job.job_id}")
    if not found or detail is None:
        # Not in the history (yet): a queued job, or one that was dequeued.
        return

    if detail.get("status") in ("completed", "failed", "cancelled"):
        apply_history(job, detail)
        job.history_fetched = True


def fetch_history_async(job_id):
    """Fetch a job's history entry in the background."""
    job = registry.get(job_id)
    if job is None or job.history_fetched:
        return
    threading.Thread(
        target=ensure_history, args=(job,), name=f"comfyui-history-{job_id[:8]}", daemon=True
    ).start()


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def create_job(workflow, client_id=None):
    """Allocate a job (and its ComfyUI prompt id) without submitting it yet."""
    job = Job(str(uuid.uuid4()), workflow, client_id)
    registry.add(job)
    return job


def submit_job(job, data):
    """Build the prompt for ``job`` and hand it to ComfyUI's queue."""
    try:
        prompt, _config = build_prompt(job.workflow, data)
    except KeyError as exc:
        job.state = STATE_ERROR
        job.error = str(exc)
        job.ended_at = time.time()
        raise

    queue_prompt(prompt, job_id=job.job_id)

    job.prompt = prompt
    job.node_ids = list(prompt.keys())
    job.finished_nodes = []
    job.progress = "Queued"
    job.state = STATE_QUEUED
    job.submitted_at = job.submitted_at or time.time()
    ensure_reconciler()
    return job


def cancel_job(job_id):
    """Cancel exactly one job. Returns ``None`` when the gateway never issued it."""
    job = registry.get(job_id)
    if job is None:
        return None

    if job.is_terminal:
        return {"job_id": job_id, "cancelled": False, "state": job.state}

    cancelled = False
    try:
        resp = requests.post(f"{COMFYUI_BASE}/api/jobs/{job_id}/cancel", timeout=REQUEST_TIMEOUT)
        if resp.ok:
            cancelled = bool(resp.json().get("cancelled"))
    except requests.RequestException:
        cancelled = False

    if cancelled:
        # A dequeued (never started) job leaves no history entry, so the local
        # state has to be set here. A running job that was interrupted also
        # reports cancelled=true.
        job.state = STATE_CANCELLED
        job.progress = "Done"
        job.step = ""
        job.ended_at = job.ended_at or time.time()
    else:
        found, detail = _comfy_get(f"/api/jobs/{job_id}")
        if found and detail:
            apply_history(job, detail)

    return {"job_id": job_id, "cancelled": cancelled, "state": job.state}


def interrupt_all():
    """Kill whatever ComfyUI is running, whoever submitted it."""
    try:
        requests.post(f"{COMFYUI_BASE}/interrupt", timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass

    # Only one prompt runs at a time, so if the gateway has a running job it is
    # the one that just got killed.
    for job in registry.active():
        if job.state == STATE_RUNNING:
            job.state = STATE_CANCELLED
            job.progress = "Done"
            job.step = ""
            job.ended_at = job.ended_at or time.time()

    return {"interrupted": True}


def reconcile_once():
    """Refresh every non-terminal job from ComfyUI's own job list."""
    reachable, payload = _comfy_get("/api/jobs?status=pending,in_progress")
    if not reachable or payload is None:
        return

    entries = payload.get("jobs") or []
    pending = sorted(
        [e for e in entries if e.get("status") == "pending"],
        key=lambda e: e.get("priority") or 0,
    )
    positions = {e.get("id"): i + 1 for i, e in enumerate(pending)}
    known = set()

    for entry in entries:
        job_id = entry.get("id")
        known.add(job_id)
        job = registry.get(job_id)
        if job is None or job.is_terminal:
            continue
        job.queue_length = len(pending)
        job.queue_position = positions.get(job_id)
        if entry.get("status") == "in_progress":
            job.state = STATE_RUNNING
            job.queue_position = None
            if job.started_at is None:
                job.started_at = time.time()
                job.last_time = job.started_at

    for job in registry.active():
        if job.job_id in known:
            continue
        found, detail = _comfy_get(f"/api/jobs/{job.job_id}")
        if not found:
            # ComfyUI unreachable - try again on the next tick.
            continue
        if detail is None:
            if time.time() - job.submitted_at > LOST_GRACE_SECONDS:
                job.state = STATE_ERROR
                job.error = "ComfyUI restarted; job lost"
                job.ended_at = time.time()
                job.progress = "Done"
            continue
        apply_history(job, detail)
        job.history_fetched = True

    # A job that finished (websocket event) still needs its history entry: it
    # carries the authoritative execution timestamps and the output list.
    for job in registry.all():
        if not job.is_terminal or job.history_fetched:
            continue
        ensure_history(job)


# ---------------------------------------------------------------------------
# Reconciler thread
# ---------------------------------------------------------------------------

_reconciler_thread = None
_reconciler_stop = threading.Event()


def ensure_reconciler():
    """Start the background reconciler (idempotent)."""
    global _reconciler_thread
    if _reconciler_thread is not None and _reconciler_thread.is_alive():
        return
    _reconciler_stop.clear()
    _reconciler_thread = threading.Thread(
        target=_reconciler_loop, name="comfyui-job-reconciler", daemon=True
    )
    _reconciler_thread.start()


def stop_reconciler():
    """Stop the background reconciler."""
    _reconciler_stop.set()


def _reconciler_loop():
    ticks = 0
    while not _reconciler_stop.is_set():
        try:
            reconcile_once()
            ticks += 1
            if ticks % SWEEP_EVERY_TICKS == 0:
                gc_input_files()
                sweep_orphan_inputs()
                sweep_old_outputs()
        except Exception:
            pass
        _reconciler_stop.wait(RECONCILE_INTERVAL)


# ---------------------------------------------------------------------------
# Garbage collection of input and output files
# ---------------------------------------------------------------------------

def gc_input_files():
    """Delete the input files of finished jobs that no running job needs."""
    active_paths = set()
    for job in registry.active():
        for path in job.input_files:
            active_paths.add(os.path.normcase(path))

    for job in registry.all():
        if job.is_active:
            continue
        for path in list(job.input_files):
            if os.path.normcase(path) in active_paths:
                continue
            if delete_input_file(path):
                job.input_files.remove(path)


def sweep_orphan_inputs(max_age=ORPHAN_MAX_AGE):
    """Delete input files that belong to no known job and are old enough."""
    known = set()
    for job in registry.all():
        for path in job.input_files:
            known.add(os.path.normcase(path))

    directory = inputs_dir()
    if not os.path.exists(directory):
        return

    now = time.time()
    for filename in os.listdir(directory):
        if not filename.endswith(INPUT_EXTENSIONS):
            continue
        path = os.path.join(directory, filename)
        if os.path.normcase(path) in known:
            continue
        try:
            mod_time = os.path.getmtime(path)
        except OSError:
            continue
        if now - mod_time > max_age:
            delete_input_file(path)


def sweep_old_outputs(max_age=ORPHAN_MAX_AGE):
    """Delete old ``api*.jpg`` outputs that no running job can still claim."""
    if not os.path.exists(comfyui_output_dir):
        return

    protected = set()
    for job in registry.active():
        start, end = job.window()
        protected.add((start, end))

    now = time.time()
    for filename in os.listdir(comfyui_output_dir):
        if not (filename.startswith("api") and filename.endswith(".jpg")):
            continue
        path = os.path.join(comfyui_output_dir, filename)
        try:
            mod_time = os.path.getmtime(path)
        except OSError:
            continue
        if now - mod_time <= max_age:
            continue
        if any(start <= mod_time <= end for start, end in protected):
            continue
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Output resolution
# ---------------------------------------------------------------------------

def find_output_files(workflow_name, window, output_dir=None):
    """Find the output files of a workflow inside a time window.

    Mirrors the legacy ``/find_workflow_outputs`` behaviour (prefix spec from
    the workflow config plus an mtime filter) but with a caller-supplied window
    instead of a global start time.
    """
    output_dir = output_dir or comfyui_output_dir
    try:
        config = load_workflow_config(workflow_name)
    except KeyError:
        return []

    output_config = config.get("output") or {}
    file_specs = output_config.get("files") or []
    if not file_specs:
        return []

    start, end = window
    found = []
    for spec in file_specs:
        prefix = spec.get("prefix", "output")
        extension = spec.get("extension", "jpg")

        if "/" in prefix or "\\" in prefix:
            parts = prefix.replace("\\", "/").split("/")
            subdir = "/".join(parts[:-1])
            filename_prefix = parts[-1]
            search_dir = os.path.join(output_dir, subdir)
        else:
            search_dir = output_dir
            filename_prefix = prefix

        if not os.path.exists(search_dir):
            continue

        for filename in os.listdir(search_dir):
            if not filename.startswith(filename_prefix):
                continue
            if not filename.endswith(f".{extension}"):
                continue
            filepath = os.path.join(search_dir, filename)
            try:
                mod_time = os.path.getmtime(filepath)
            except OSError:
                continue
            if start is not None and mod_time < start:
                continue
            if end is not None and mod_time > end:
                continue
            found.append({
                "filename": filename,
                "filepath": filepath,
                "mod_time": mod_time,
                "extension": extension,
                "prefix": prefix,
                "subfolder": os.path.relpath(search_dir, output_dir).replace('\\', '/')
                if os.path.normcase(search_dir) != os.path.normcase(output_dir) else "",
                "media_type": None,
                "type": "output",
            })

    found.sort(key=lambda f: f["mod_time"], reverse=True)
    return found


def resolve_outputs(job):
    """Resolve the output files of a job.

    Primary source is ComfyUI's history entry for the job (exact). The prefix
    based search is the fallback for workflows whose history entry has already
    been evicted or whose outputs are not reported by ComfyUI.
    """
    ensure_history(job)
    if job.outputs:
        return [
            {
                "filename": out["filename"],
                "filepath": out["filepath"],
                "mod_time": None,
                "extension": os.path.splitext(out["filename"])[1].lstrip("."),
                "prefix": "",
                "subfolder": out.get("subfolder", ""),
                "media_type": out.get("media_type"),
                "type": out.get("type"),
            }
            for out in job.outputs
        ]
    return find_output_files(job.workflow, job.window())


def resolve_processed_images(job):
    """The legacy ``api*.jpg`` images produced by a job."""
    ensure_history(job)
    images = []
    start, end = job.window()
    if not os.path.exists(comfyui_output_dir):
        return images

    for filename in os.listdir(comfyui_output_dir):
        if not (filename.startswith("api") and filename.endswith(".jpg")):
            continue
        jpg_path = os.path.join(comfyui_output_dir, filename)
        try:
            mod_time = os.path.getmtime(jpg_path)
        except OSError:
            continue
        if not (start <= mod_time <= end):
            continue
        try:
            with open(jpg_path, "rb") as handle:
                images.append((mod_time, filename, handle.read()))
        except OSError:
            continue

    images.sort(key=lambda item: item[0])
    return [base64.b64encode(data).decode("ascii") for _mod, _name, data in images]
