import base64
import os
from io import BytesIO

from PIL import Image

from comfyui_config import COMFYUI_PATH

INPUT_EXTENSIONS = (".jpg", ".mp4", ".webm", ".mp3", ".wav", ".webp", ".png")


def inputs_dir():
    return os.path.join(COMFYUI_PATH, 'input')


def is_valid_base64_image(base64_data):
    try:
        decoded_data = base64.b64decode(base64_data)
    except (base64.binascii.Error, ValueError, TypeError):
        return False

    try:
        with Image.open(BytesIO(decoded_data)) as img:
            img.verify()
            img = Image.open(BytesIO(decoded_data))
            img.getdata()[0]
        return True
    except Exception:
        return False


def find_existing_file(prefix, file_bytes, job_id=None):
    """Check if a file with the same prefix and content already exists.

    ``job_id`` restricts the search to the files owned by that job. Without it
    any file with the same prefix and identical bytes is reused - the caller
    has to register the reused path on the new job so the garbage collector
    does not delete it while the new job still needs it.
    """
    inputs_path = inputs_dir()
    if not os.path.exists(inputs_path):
        return None

    prefix_pattern = prefix + "_"
    for filename in os.listdir(inputs_path):
        if not filename.startswith(prefix_pattern):
            continue
        if job_id and not filename.startswith(f"{prefix}_{job_id}"):
            continue
        filepath = os.path.join(inputs_path, filename)
        try:
            with open(filepath, 'rb') as f:
                if f.read() == file_bytes:
                    return filepath
        except Exception:
            continue
    return None


def save_base64_image(base64_data, prefix, job_id):
    """Write a base64 image into ComfyUI's input directory, owned by ``job_id``."""
    decoded_data = base64.b64decode(base64_data)
    inputs_path = inputs_dir()
    os.makedirs(inputs_path, exist_ok=True)
    save_path = os.path.join(inputs_path, f"{prefix}_{job_id}.jpg")
    with open(save_path, 'wb') as img_file:
        img_file.write(decoded_data)
    return save_path


def save_input_file(file_bytes, prefix, job_id, extension=""):
    """Write an uploaded file into ComfyUI's input directory, owned by ``job_id``."""
    inputs_path = inputs_dir()
    os.makedirs(inputs_path, exist_ok=True)
    ext = extension or ""
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    save_path = os.path.join(inputs_path, f"{prefix}_{job_id}{ext}")
    with open(save_path, 'wb') as handle:
        handle.write(file_bytes)
    return save_path


def delete_input_file(filepath):
    """Delete one input file. Never raises for a missing file."""
    try:
        if filepath and os.path.isfile(filepath):
            os.remove(filepath)
            return True
    except OSError:
        pass
    return False
