"""Websocket client for ComfyUI's execution events.

Messages are pushed onto a queue by the websocket callback and processed by a
single consumer thread, so events are handled in the order ComfyUI sent them.
Every event is routed to a job by its ``prompt_id``, which is the gateway job
id - the gateway no longer keeps a single global "current job" slot.
"""
import json
import queue
import threading
import time

import websocket

import comfyui_state as st
from comfyui_config import comfyui_server
from comfyui_state import client_id

import comfyui_jobs as jobs

_message_queue = queue.Queue()
_consumer_thread = None
_supervisor_thread = None
_shutdown = threading.Event()
_ws_lock = threading.RLock()


def format_time(seconds):
    """Convert seconds to mm:ss format."""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def open_websocket_connection():
    """Open the ComfyUI websocket (idempotent) and start the message consumer."""
    global _consumer_thread

    with _ws_lock:
        if st.ws is not None:
            return st.ws
        app = websocket.WebSocketApp(
            f"ws://{comfyui_server}/ws?clientId={client_id}",
            on_message=ws_message,
            on_close=_on_close,
            on_error=_on_error,
        )
        st.ws = app

    threading.Thread(target=app.run_forever, daemon=True, name="comfyui-ws").start()
    _ensure_consumer()
    _ensure_supervisor()
    return app


def stop_websocket_connection():
    """Close the websocket and stop the background threads."""
    _shutdown.set()
    with _ws_lock:
        app = st.ws
        st.ws = None
    if app is not None:
        try:
            app.close()
        except Exception:
            pass
    _message_queue.put(None)


def _on_close(_ws, *_args):
    with _ws_lock:
        st.ws = None


def _on_error(_ws, _error):
    # The close handler does the cleanup; swallow the error so websocket-client
    # does not print a traceback for an ordinary disconnect.
    pass


def _ensure_consumer():
    global _consumer_thread
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return
    _consumer_thread = threading.Thread(
        target=_consume_loop, name="comfyui-ws-consumer", daemon=True
    )
    _consumer_thread.start()


def _ensure_supervisor():
    """Keep the websocket alive: reconnect with backoff after a drop."""
    global _supervisor_thread
    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return
    _shutdown.clear()
    _supervisor_thread = threading.Thread(
        target=_supervise_loop, name="comfyui-ws-supervisor", daemon=True
    )
    _supervisor_thread.start()


def _supervise_loop():
    backoff = 1.0
    while not _shutdown.is_set():
        with _ws_lock:
            connected = st.ws is not None
        if connected:
            _shutdown.wait(0.5)
            continue

        try:
            open_websocket_connection()
            backoff = 1.0
            continue
        except Exception:
            pass

        _shutdown.wait(backoff)
        backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

def ws_message(_ws, msg):
    """Websocket callback - only enqueues, the consumer thread does the work."""
    _message_queue.put(msg)


def _consume_loop():
    while True:
        item = _message_queue.get()
        try:
            if item is None:
                break
            _handle_message(item)
        except Exception:
            pass
        finally:
            _message_queue.task_done()


def _handle_message(msg):
    if isinstance(msg, bytes):
        try:
            decoded_msg = msg.decode('utf-8')
        except UnicodeDecodeError:
            return
    else:
        decoded_msg = msg

    try:
        message = json.loads(decoded_msg)
    except (json.JSONDecodeError, TypeError):
        return

    if not isinstance(message, dict):
        return

    msg_type = message.get('type')
    data = message.get('data')
    if not isinstance(data, dict):
        data = {}

    if msg_type == 'progress':
        _handle_progress(data)
    elif msg_type == 'execution_cached':
        _handle_execution_cached(data)
    elif msg_type == 'executing':
        _handle_executing(data)
    elif msg_type == 'execution_success':
        _finish_job(data.get('prompt_id'), jobs.STATE_DONE)
    elif msg_type == 'execution_error':
        _finish_job(data.get('prompt_id'), jobs.STATE_ERROR,
                    error=data.get('exception_message'))
    elif msg_type == 'execution_interrupted':
        _finish_job(data.get('prompt_id'), jobs.STATE_CANCELLED)


def _job_for(prompt_id):
    return jobs.registry.get(prompt_id)


def _handle_progress(data):
    job = _job_for(data.get('prompt_id'))
    if job is None:
        return

    current_step = data.get('value')
    max_step = data.get('max')
    if current_step is None or max_step is None:
        return

    if current_step == max_step:
        job.step = ""
        return

    if job.last_node == job.cur_node and job.last_time:
        elapsed_time = time.time() - job.last_time
        avg_time_per_step = elapsed_time
        if current_step > job.last_step:
            avg_time_per_step = elapsed_time / (current_step - job.last_step)
        elif current_step > 0:
            avg_time_per_step = elapsed_time / current_step
        remaining_steps = max_step - current_step
        estimated_time = remaining_steps * avg_time_per_step
        job.step = (
            f"--> Step: {current_step}/{max_step} "
            f"[{format_time(avg_time_per_step * current_step)}<{format_time(estimated_time)}]"
        )
    else:
        job.last_node = job.cur_node
        job.last_time = time.time()
        job.last_step = current_step
        job.step = f"--> Step: {current_step}/{max_step}"


def _handle_execution_cached(data):
    job = _job_for(data.get('prompt_id'))
    if job is None:
        return

    for node in data.get('nodes') or []:
        if node not in job.finished_nodes:
            job.finished_nodes.append(node)
    job.progress = f"{len(job.finished_nodes)} of {len(job.node_ids)} nodes processed."


def _handle_executing(data):
    job = _job_for(data.get('prompt_id'))
    if job is None:
        return

    node = data.get('node')
    if node is None:
        job.step = ""
        job.progress = "Done"
        job.state = jobs.STATE_DONE
        job.ended_at = job.ended_at or time.time()
        jobs.fetch_history_async(job.job_id)
        return

    if node in job.finished_nodes:
        return

    job.finished_nodes.append(node)
    job.cur_node = node
    job.state = jobs.STATE_RUNNING
    if job.started_at is None:
        job.started_at = time.time()
        job.last_time = job.started_at
    node_info = (job.prompt or {}).get(node, {})
    title = node_info.get('_meta', {}).get('title') if isinstance(node_info, dict) else None
    label = title if title else node
    job.progress = f"Processing \"{label}\" ({len(job.finished_nodes)}/{len(job.node_ids)})..."


def _finish_job(prompt_id, state, error=None):
    job = _job_for(prompt_id)
    if job is None:
        return
    if job.is_terminal:
        return
    job.state = state
    job.progress = "Done"
    job.step = ""
    job.ended_at = job.ended_at or time.time()
    if error:
        job.error = error
    jobs.fetch_history_async(job.job_id)
