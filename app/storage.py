"""Uploaded files on the public disk, served at /storage/<path> like Laravel's `storage:link`."""

from pathlib import Path

from starlette.datastructures import UploadFile

from app.auth import random_string
from app.config import get_settings
from app.serial import is_external
from app.validation import image_type

EXTENSIONS = {"jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif", "bmp": "bmp"}


def root() -> Path:
    return Path(get_settings().storage_dir)


def store(upload: UploadFile, folder: str) -> str:
    """Save the upload under a random name and return its path on the disk, such as "products/4/<name>.jpg"."""
    extension = EXTENSIONS.get(image_type(upload) or "", Path(upload.filename or "").suffix.lstrip(".") or "bin")
    path = f"{folder.strip('/')}/{random_string(40)}.{extension}"
    target = root() / path
    target.parent.mkdir(parents=True, exist_ok=True)
    upload.file.seek(0)
    with target.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            out.write(chunk)
    return path


def delete(path: str | None) -> None:
    """Remove an uploaded file. External URLs are left alone."""
    if not path or is_external(path):
        return
    target = (root() / path).resolve()
    if root().resolve() in target.parents and target.is_file():
        target.unlink()


def exists(path: str) -> bool:
    return (root() / path).is_file()
