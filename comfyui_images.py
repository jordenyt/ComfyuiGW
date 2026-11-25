import base64
import hashlib
import os
from io import BytesIO

from PIL import Image

from comfyui_config import COMFYUI_PATH

INPUT_EXTENSIONS = (".jpg", ".mp4", ".webm", ".mp3", ".wav", ".webp", ".png")

# Length of the content hash that identifies a cached input file.
CACHE_HASH_LENGTH = 16

# PIL format -> file extension for images that arrive as base64.
_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "GIF": ".gif",
}


def inputs_dir():
    return os.path.join(COMFYUI_PATH, 'input')


def content_hash(file_bytes):
    """Hash of the file content - the identity of a cached input file."""
    return hashlib.sha256(file_bytes).hexdigest()[:CACHE_HASH_LENGTH]


def cache_filename(prefix, file_hash, extension=""):
    """The content-addressed name of a cached input file.

    The same bytes always get the same name, so ComfyUI sees an unchanged
    input for a repeated upload and can reuse its cached node output.
    """
    ext = extension or ""
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return f"{prefix}_{file_hash}{ext}"


def is_cache_filename(filename):
    """True if the file name is content-addressed (``<prefix>_<16 hex>``)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    _, separator, tail = stem.rpartition("_")
    if not separator or len(tail) != CACHE_HASH_LENGTH:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in tail)


def is_cache_path(path):
    """True if the path points at a content-addressed cache file."""
    return is_cache_filename(path)


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
    """Return the cached input file that holds exactly these bytes.

    The lookup is by content hash, so the same upload always resolves to the
    same file name. ComfyUI keys its node cache on that name, which is what
    makes a repeated upload reuse the cached image/video instead of decoding
    it again. ``job_id`` is accepted for backwards compatibility and ignored.
    """
    directory = inputs_dir()
    if not os.path.exists(directory):
        return None

    stem = f"{prefix}_{content_hash(file_bytes)}"
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None

    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if os.path.splitext(entry.name)[0] != stem:
            continue
        try:
            with open(entry.path, 'rb') as handle:
                if handle.read() == file_bytes:
                    return entry.path
        except OSError:
            continue
    return None


def save_base64_image(base64_data, prefix, job_id=None):
    """Write a base64 image into ComfyUI's input directory under its cache name."""
    decoded_data = base64.b64decode(base64_data)
    return save_input_file(decoded_data, prefix, extension=detect_image_extension(decoded_data))


def detect_image_extension(image_bytes):
    """The extension that matches the actual image format of the bytes."""
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            return _FORMAT_EXTENSIONS.get((img.format or "").upper(), ".jpg")
    except Exception:
        return ".jpg"


def save_input_file(file_bytes, prefix, job_id=None, extension=""):
    """Write an uploaded file into ComfyUI's input directory under its cache name.

    The file is named after the hash of its content, so identical uploads
    share one file and one ComfyUI cache entry. ``job_id`` is accepted for
    backwards compatibility and ignored.
    """
    inputs_path = inputs_dir()
    os.makedirs(inputs_path, exist_ok=True)

    ext = extension or ""
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    save_path = os.path.join(inputs_path, cache_filename(prefix, content_hash(file_bytes), ext))

    if os.path.isfile(save_path):
        # Keep the existing file (and its mtime) so ComfyUI's cache stays valid.
        return save_path

    tmp_path = f"{save_path}.part"
    with open(tmp_path, 'wb') as handle:
        handle.write(file_bytes)
    os.replace(tmp_path, save_path)
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
