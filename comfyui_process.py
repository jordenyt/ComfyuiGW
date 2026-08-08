import os
import platform
import shlex
import subprocess

import psutil

from comfyui_config import COMFYUI_PATH, is_windows, local_path
import comfyui_state as st

comfyui_process = None


def start_comfyui():
    global comfyui_process
    if comfyui_process:
        return {"message": "ComfyUI already started"}

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


def stop_comfyui():
    if not comfyui_process:
        return {"message": "ComfyUI not started"}

    if st.ws:
        st.ws.close()
        st.ws = None

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

    return {"message": "ComfyUI Stopped."}
