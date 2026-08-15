from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    """Write text beside its destination and publish it with one atomic rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(value)
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def temporary_output_path(destination: Path) -> Path:
    """Return a unique sibling path whose final suffix still identifies the format."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
    )


def atomic_copy(source: Path, destination: Path) -> Path:
    """Copy a file without ever exposing a partially copied destination."""

    temporary = temporary_output_path(destination)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_filename(value: str, fallback: str = "video") -> str:
    cleaned = "".join(char if char.isalnum() or char in " ._-()[]" else "_" for char in value)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:120] or fallback


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
