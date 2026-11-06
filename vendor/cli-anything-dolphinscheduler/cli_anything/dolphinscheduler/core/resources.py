"""Resource Center operations for DolphinScheduler.

The Resource Center API is global to the authenticated user/tenant, not scoped
under a project. These helpers wrap the real ``/resources`` controller and keep
file-name splitting, multipart upload, and binary download rules in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .client import DolphinSchedulerClient

RESOURCE_TYPES = ("FILE", "ALL")
_RESOURCES = "/resources"


def list_tree(client: DolphinSchedulerClient, resource_type: str = "FILE") -> list[dict[str, Any]]:
    """Return the full resource tree for a resource type."""
    return client.get(f"{_RESOURCES}/list", params={"type": _normalize_type(resource_type)}) or []


def list_items(
    client: DolphinSchedulerClient,
    full_name: str,
    *,
    resource_type: str = "FILE",
    search_val: Optional[str] = None,
    page_no: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Return one paged directory listing from Resource Center."""
    _require_text("full_name", full_name)
    if page_no < 1:
        raise ValueError("page_no must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    return client.get(
        _RESOURCES,
        params={
            "type": _normalize_type(resource_type),
            "fullName": full_name,
            "searchVal": search_val,
            "pageNo": page_no,
            "pageSize": page_size,
        },
    )


def base_dir(client: DolphinSchedulerClient, resource_type: str = "FILE") -> str:
    """Return the Resource Center base directory for the current user/tenant."""
    return str(client.get(f"{_RESOURCES}/base-dir", params={"type": _normalize_type(resource_type)}))


def create_directory(
    client: DolphinSchedulerClient,
    name: str,
    current_dir: str,
    *,
    resource_type: str = "FILE",
) -> Any:
    """Create a directory under ``current_dir``."""
    _require_text("name", name)
    _require_text("current_dir", current_dir)
    return client.post(
        f"{_RESOURCES}/directory",
        data={
            "type": _normalize_type(resource_type),
            "name": name,
            "currentDir": current_dir,
        },
    )


def create_file_from_content(
    client: DolphinSchedulerClient,
    name: str,
    content: str,
    current_dir: str,
    *,
    resource_type: str = "FILE",
) -> Any:
    """Create a Resource Center file from inline text content."""
    stem, suffix = _split_resource_file_name(name)
    _require_text("current_dir", current_dir)
    return client.post(
        f"{_RESOURCES}/online-create",
        data={
            "type": _normalize_type(resource_type),
            "fileName": stem,
            "suffix": suffix,
            "content": content,
            "currentDir": current_dir,
        },
    )


def upload_file(
    client: DolphinSchedulerClient,
    path: str,
    current_dir: str,
    *,
    name: Optional[str] = None,
    resource_type: str = "FILE",
) -> Any:
    """Upload a local file into Resource Center."""
    local_path = Path(path)
    if not local_path.is_file():
        raise ValueError(f"local file does not exist: {path}")
    resource_name = name or local_path.name
    _require_text("current_dir", current_dir)
    _split_resource_file_name(resource_name)
    with local_path.open("rb") as file_obj:
        return client.post(
            _RESOURCES,
            data={
                "type": _normalize_type(resource_type),
                "name": resource_name,
                "currentDir": current_dir,
            },
            files={"file": (resource_name, file_obj)},
        )


def update_file_content(client: DolphinSchedulerClient, full_name: str, content: str) -> Any:
    """Replace a Resource Center file with inline text content."""
    _require_text("full_name", full_name)
    return client.put(
        f"{_RESOURCES}/update-content",
        data={"fullName": full_name, "content": content},
    )


def update_file(
    client: DolphinSchedulerClient,
    full_name: str,
    path: str,
    *,
    name: Optional[str] = None,
) -> Any:
    """Replace a Resource Center file with a local file upload."""
    _require_text("full_name", full_name)
    local_path = Path(path)
    if not local_path.is_file():
        raise ValueError(f"local file does not exist: {path}")
    resource_name = name or local_path.name
    _split_resource_file_name(resource_name)
    with local_path.open("rb") as file_obj:
        return client.put(
            _RESOURCES,
            data={"fullName": full_name, "name": resource_name},
            files={"file": (resource_name, file_obj)},
        )


def rename_resource(client: DolphinSchedulerClient, full_name: str, name: str) -> Any:
    """Rename a Resource Center file or directory."""
    _require_text("full_name", full_name)
    _require_text("name", name)
    return client.put(_RESOURCES, data={"fullName": full_name, "name": name})


def delete_resource(client: DolphinSchedulerClient, full_name: str) -> Any:
    """Delete a Resource Center file or directory."""
    _require_text("full_name", full_name)
    return client.delete(_RESOURCES, params={"fullName": full_name})


def view_file(
    client: DolphinSchedulerClient,
    full_name: str,
    *,
    skip_line_num: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Fetch text content for a Resource Center file."""
    _require_text("full_name", full_name)
    if skip_line_num < 0:
        raise ValueError("skip_line_num must be >= 0")
    if limit == 0 or limit < -1:
        raise ValueError("limit must be -1 or >= 1")
    return client.get(
        f"{_RESOURCES}/view",
        params={
            "fullName": full_name,
            "skipLineNum": skip_line_num,
            "limit": limit,
        },
    )


def download_resource(client: DolphinSchedulerClient, full_name: str) -> bytes:
    """Download a Resource Center file or directory archive as bytes."""
    _require_text("full_name", full_name)
    return client.download(f"{_RESOURCES}/download", params={"fullName": full_name})


def _normalize_type(resource_type: str) -> str:
    value = str(resource_type).strip().upper()
    if value not in RESOURCE_TYPES:
        raise ValueError(f"Invalid resource_type={resource_type!r}; expected FILE or ALL")
    return value


def _split_resource_file_name(name: str) -> tuple[str, str]:
    _require_text("name", name)
    path = Path(name)
    if path.name != name:
        raise ValueError("name must be a file name, not a path")
    if not path.suffix or path.stem == "":
        raise ValueError("resource file name must include a suffix, e.g. demo.py")
    return path.stem, path.suffix.lstrip(".")


def _require_text(field_name: str, value: str) -> None:
    if not str(value).strip():
        raise ValueError(f"{field_name} must not be empty")
