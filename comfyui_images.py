import base64
import os
from io import BytesIO

from PIL import Image

from comfyui_config import COMFYUI_PATH


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


def find_existing_file(prefix, file_bytes):
    """Check if a file with the same prefix and content already exists"""
    inputs_path = os.path.join(COMFYUI_PATH, 'input')
    if not os.path.exists(inputs_path):
        return None

    prefix_pattern = prefix + "_"
    for filename in os.listdir(inputs_path):
        if filename.startswith(prefix_pattern):
            filepath = os.path.join(inputs_path, filename)
            try:
                with open(filepath, 'rb') as f:
                    if f.read() == file_bytes:
                        return filepath
            except Exception:
                continue
    return None


def save_base64_image(base64_data, prefix, session_id):
    decoded_data = base64.b64decode(base64_data)
    save_path = os.path.join(COMFYUI_PATH, 'input', f"{prefix}_{session_id}.jpg")
    with open(save_path, 'wb') as img_file:
        img_file.write(decoded_data)
    return save_path


def cleanup_images(prefixes):
    inputs_path = os.path.join(COMFYUI_PATH, 'input')
    if os.path.exists(inputs_path):
        for f in os.listdir(inputs_path):
            for prefix in prefixes:
                if f.startswith(prefix) and (f.endswith(".jpg") or f.endswith(".mp4") or f.endswith(".webm") or f.endswith(".mp3") or f.endswith(".wav") or f.endswith(".webp") or f.endswith(".png")):
                    os.remove(os.path.join(inputs_path, f))
