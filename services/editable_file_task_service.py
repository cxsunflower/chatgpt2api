from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

from services.account_service import (
    RefreshCredentialsChangedError,
    TerminalRefreshTokenError,
    account_service,
)
from services.config import DATA_DIR
from services.content_filter import request_text
from services.editable_file_failure import EditableFileFailureError
from services.log_service import LOG_TYPE_CALL, log_service
from services.native_file_support import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    NativeFileError,
    clean_generated_text,
    decode_file_part,
    file_name,
    repair_generated_code,
    validate_download,
)
from services.openai_backend_api import EDITABLE_FILE_MODEL, OpenAIBackendAPI
from services.storage.editable_file_task_repository import EditableFileTaskRepository
from utils.file_names import is_safe_public_filename
from utils.helper import new_uuid
from utils.timezone import beijing_from_timestamp, beijing_now_str

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
TASK_STATUS_PRESENTATION = {
    TASK_STATUS_QUEUED: {
        "label": "等待中",
        "tone": "muted",
        "icon": "lucide:loader-circle",
    },
    TASK_STATUS_RUNNING: {
        "label": "生成中",
        "tone": "warning",
        "icon": "lucide:loader-circle",
    },
    TASK_STATUS_SUCCESS: {
        "label": "已完成",
        "tone": "success",
        "icon": "lucide:file-check-2",
    },
    TASK_STATUS_ERROR: {
        "label": "失败",
        "tone": "danger",
        "icon": "lucide:circle-alert",
    },
}
EDITABLE_FILE_PLAN_TYPES = ("Plus", "Team", "Pro", "Enterprise")
EDITABLE_FILE_TIMEOUT_SECS = 1200.0
EDITABLE_FILE_ROOT = DATA_DIR / "files"
EDITABLE_FILE_TASK_ID_MAX_LENGTH = 160
EDITABLE_FILE_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
EDITABLE_FILE_STORAGE_PREFIX = "asset-"
EDITABLE_FILE_EXTENSIONS = {
    "ppt": {".ppt", ".pptx", ".zip"},
    "psd": {".psd", ".zip"},
    "file": set(),
}


def _now_iso() -> str:
    return beijing_now_str()


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _resolve_task_id(client_task_id: str) -> str:
    task_id = _clean(client_task_id)
    if not task_id:
        return new_uuid()
    if (
        len(task_id) > EDITABLE_FILE_TASK_ID_MAX_LENGTH
        or task_id in {".", ".."}
        or EDITABLE_FILE_TASK_ID_PATTERN.fullmatch(task_id) is None
    ):
        raise EditableFileTaskInvalidIdError(
            "client_task_id must be 1-160 characters using letters, numbers, '.', '_' or '-'"
        )
    return task_id


def _is_storage_id(value: object) -> bool:
    storage_id = _clean(value)
    if not storage_id.startswith(EDITABLE_FILE_STORAGE_PREFIX):
        return False
    try:
        uuid_value = storage_id.removeprefix(EDITABLE_FILE_STORAGE_PREFIX)
        return str(UUID(uuid_value)) == uuid_value
    except (ValueError, AttributeError):
        return False


def _storage_id(task: dict[str, Any]) -> str:
    storage_id = _clean(task.get("storage_id"))
    if not _is_storage_id(storage_id):
        raise ValueError("editable file task storage id is invalid")
    return storage_id


def _elapsed_seconds(task: dict[str, Any]) -> int:
    start = float(task.get("started_ts") or task.get("created_ts") or 0)
    end = float(task.get("ended_ts") or time.time())
    return max(0, int(end - start)) if start else 0


def _file_url(path: Path, base_url: str) -> str:
    rel = path.resolve().relative_to(EDITABLE_FILE_ROOT.resolve()).as_posix()
    prefix = str(base_url or "").strip().rstrip("/")
    return f"{prefix}/files/{quote(rel, safe='/')}" if prefix else f"/files/{quote(rel, safe='/')}"


def _editable_access_token() -> str:
    attempted_tokens: set[str] = set()
    while True:
        accounts = [
            item for item in account_service.list_accounts()
            if (token := _clean(item.get("access_token")))
               and token not in attempted_tokens
               and account_service._is_account_selectable(item, allow_limited=True)
               and account_service._account_matches_any_plan_type(item, EDITABLE_FILE_PLAN_TYPES)
        ]
        if not accounts:
            raise RuntimeError("no available plus/team/pro account")
        accounts.sort(key=lambda item: _clean(item.get("last_used_at")))
        token = _clean(accounts[0].get("access_token"))
        attempted_tokens.add(token)
        try:
            return account_service.ensure_access_token(token, event="editable_file_task")
        except (TerminalRefreshTokenError, RefreshCredentialsChangedError):
            continue


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    raw_status = _clean(task.get("status"), TASK_STATUS_ERROR)
    status = raw_status if raw_status in TASK_STATUS_PRESENTATION else TASK_STATUS_ERROR
    presentation = TASK_STATUS_PRESENTATION[status]
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    delete_pending = bool(task.get("delete_pending"))
    can_download = not delete_pending and status == TASK_STATUS_SUCCESS and bool(
        _clean(result.get("primary_url")) or _clean(result.get("zip_url"))
        or any(isinstance(item, dict) and _clean(item.get("url")) for item in result.get("files") or [])
    )
    item = {
        "id": task.get("id"),
        "status": status,
        "status_label": presentation["label"],
        "status_tone": presentation["tone"],
        "status_icon": presentation["icon"],
        "is_active": status in UNFINISHED_STATUSES,
        "kind": task.get("kind"),
        "model": task.get("model"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "elapsed_seconds": _elapsed_seconds(task),
        "can_download": can_download,
        "can_delete": status in TERMINAL_STATUSES,
    }
    for key in ("result", "error"):
        if task.get(key):
            item[key] = task[key]
    return item


class EditableFileTaskNotFoundError(LookupError):
    pass


class EditableFileTaskNotTerminalError(RuntimeError):
    pass


class EditableFileTaskInvalidIdError(ValueError):
    pass


class EditableFileTaskConflictError(ValueError):
    pass


class EditableFileTaskCleanupError(RuntimeError):
    pass


class EditableFileTaskService:
    def __init__(
        self,
        *,
        repository: EditableFileTaskRepository | None = None,
        database_url: str | None = None,
    ) -> None:
        if repository is not None and database_url is not None:
            raise ValueError("provide repository or database_url, not both")
        self._lock = threading.RLock()
        self._repository = repository or EditableFileTaskRepository(database_url)
        with self._lock:
            self._recover_pending_deletions_locked()
            self._recover_unfinished_locked()

    def submit_ppt(self, identity: dict[str, object], *, client_task_id: str = "", prompt: str = "", base64_images: list[str] | None = None, base_url: str = "", model: str = EDITABLE_FILE_MODEL) -> dict[str, Any]:
        return self._submit(identity, client_task_id=client_task_id, kind="ppt", prompt=prompt, base64_images=base64_images or [], base_url=base_url, model=model)

    def submit_psd(self, identity: dict[str, object], *, client_task_id: str = "", prompt: str = "", base64_images: list[str] | None = None, base_url: str = "", model: str = EDITABLE_FILE_MODEL) -> dict[str, Any]:
        return self._submit(identity, client_task_id=client_task_id, kind="psd", prompt=prompt, base64_images=base64_images or [], base_url=base_url, model=model)

    def submit_file(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str = "",
        prompt: str = "",
        model: str = EDITABLE_FILE_MODEL,
        output_filename: str = "result.txt",
        base_url: str = "",
        input_files: list | None = None,
        transport: str = "editable",
        thinking_effort: str = "",
        output_bundle: str = "zip",
    ) -> dict[str, Any]:
        if transport not in {"editable", "chat"}:
            raise EditableFileTaskInvalidIdError("invalid generation transport")
        if output_bundle not in {"separate", "zip"}:
            raise EditableFileTaskInvalidIdError("invalid output bundle mode")
        if model == "auto" or not model.strip() or not prompt.strip():
            raise EditableFileTaskInvalidIdError("nonempty prompt and explicit model are required")
        try:
            output_filename = file_name(output_filename)
            originals = [decode_file_part(item) for item in (input_files or [])]
        except NativeFileError as exc:
            raise EditableFileTaskInvalidIdError(str(exc)) from exc
        if len(originals) > MAX_FILES or sum(len(item["data"]) for item in originals) > MAX_TOTAL_BYTES:
            raise EditableFileTaskInvalidIdError("native_file:input_quota_exceeded")
        return self._submit(
            identity,
            client_task_id=client_task_id,
            kind="file",
            prompt=prompt,
            base64_images=[],
            base_url=base_url,
            model=model,
            output_filename=output_filename,
            input_files=input_files or [],
            transport=transport,
            thinking_effort=thinking_effort,
            output_bundle=output_bundle,
        )

    def list_tasks(
            self,
            identity: dict[str, object],
            task_ids: list[str],
            *,
            limit: int = 0,
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested = [_clean(item) for item in task_ids if _clean(item)]
        with self._lock:
            if requested:
                stored = self._repository.list_for_owner(owner, task_ids=requested)
                by_id = {_clean(task.get("id")): task for task in stored}
                items = [by_id[task_id] for task_id in requested if task_id in by_id]
                return {
                    "items": [_public_task(item) for item in items],
                    "missing_ids": [task_id for task_id in requested if task_id not in by_id],
                }
            items = self._repository.list_for_owner(owner, limit=limit)
        return {"items": [_public_task(item) for item in items], "missing_ids": []}

    def delete_task(
            self,
            identity: dict[str, object],
            task_id: str,
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        normalized_task_id = _clean(task_id)
        with self._lock:
            task = self._repository.get(owner, normalized_task_id)
            if task is None:
                raise EditableFileTaskNotFoundError("editable file task not found")
            if task.get("status") not in TERMINAL_STATUSES:
                raise EditableFileTaskNotTerminalError(
                    "editable file task is not terminal"
                )

            if not task.get("delete_pending"):
                task = self._repository.update(
                    owner,
                    normalized_task_id,
                    delete_pending=True,
                ) or task

            self._remove_task_files(task)
            self._repository.delete(owner, normalized_task_id)
        return {"task_id": normalized_task_id, "deleted": True}

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        kind: str,
        prompt: str,
        base64_images: list[str],
        base_url: str,
        model: str = EDITABLE_FILE_MODEL,
        output_filename: str = "",
        input_files: list | None = None,
        transport: str = "editable",
        thinking_effort: str = "",
        output_bundle: str = "zip",
    ) -> dict[str, Any]:
        task_id = _resolve_task_id(client_task_id)
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        fingerprint_data = {
            "kind": kind,
            "prompt": prompt,
            "images": base64_images,
            "model": model,
            "output_filename": output_filename,
            "input_files": input_files or [],
            "thinking_effort": thinking_effort,
            "output_bundle": output_bundle,
        }
        if transport == "chat":
            fingerprint_data["transport"] = "chat"
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self._lock:
            ts = time.time()
            task, created = self._repository.create({
                "id": task_id,
                "storage_id": f"{EDITABLE_FILE_STORAGE_PREFIX}{new_uuid()}",
                "owner_id": owner,
                "status": TASK_STATUS_QUEUED,
                "kind": kind,
                "model": model,
                "request_fingerprint": fingerprint,
                "output_filename": output_filename,
                "created_at": now,
                "updated_at": now,
                "created_ts": ts,
                "updated_ts": ts,
            })
            if not created:
                if task.get("request_fingerprint") != fingerprint:
                    raise EditableFileTaskConflictError(
                        "client_task_id already belongs to a different input"
                    )
                return _public_task(task)
        threading.Thread(
            target=self._run_task,
            args=(key, kind, prompt, base64_images, dict(identity), base_url, input_files or []),
            kwargs={
                "transport": transport,
                "thinking_effort": thinking_effort,
                "output_bundle": output_bundle,
            },
            name=f"{kind}-file-task-{task_id[:16]}",
            daemon=True,
        ).start()
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        kind: str,
        prompt: str,
        base64_images: list[str],
        identity: dict[str, object],
        base_url: str,
        input_files: list | None = None,
        transport: str = "editable",
        thinking_effort: str = "",
        output_bundle: str = "zip",
    ) -> None:
        started = time.time()
        token = ""
        account_email = ""
        staging_dir: Path | None = None
        published_dir: Path | None = None
        success_recorded = False
        stored_task: dict[str, Any] = {}
        self._update_task(key, status=TASK_STATUS_RUNNING, error="", started_ts=started)
        try:
            if kind == "psd" and not base64_images:
                raise ValueError("base64_images is empty")
            token = _editable_access_token()
            account = account_service.get_account(token) or {}
            account_email = _clean(account.get("email"))
            with self._lock:
                owner_id, task_id = key.rsplit(":", 1)
                stored_task = self._repository.get(owner_id, task_id) or {}
            if not stored_task:
                return
            staging_dir = self._staging_dir(stored_task)
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            with OpenAIBackendAPI(token) as backend:
                if kind == "file":
                    result = backend.export_file(
                        prompt,
                        staging_dir,
                        model=stored_task["model"],
                        output_filename=stored_task.get("output_filename") or "result.txt",
                        input_files=input_files or [],
                        timeout_secs=EDITABLE_FILE_TIMEOUT_SECS,
                        transport=transport,
                        thinking_effort=thinking_effort,
                    )
                else:
                    export = backend.export_psd_zip if kind == "psd" else backend.export_ppt_zip
                    result = export(
                        base64_images,
                        prompt,
                        staging_dir,
                        timeout_secs=EDITABLE_FILE_TIMEOUT_SECS,
                    )
            account_service.mark_text_used(token)
            if kind == "file":
                if not getattr(result, "files", []):
                    text = clean_generated_text(getattr(result, "text", ""))
                    if transport == "chat" or not text:
                        raise NativeFileError("generation_no_actual_file")
                    data = {
                        "conversation_id": getattr(result, "conversation_id", ""),
                        "primary_url": "",
                        "files": [],
                        "original_files": [],
                        "text": text,
                        "delivery_format": "text_only",
                    }
                    published_dir = None
                else:
                    data = self._publish_native_output(
                        stored_task,
                        result,
                        base_url=base_url,
                        output_bundle=output_bundle,
                    )
                    published_dir = self._output_dir(stored_task)
            else:
                primary_path, zip_path = self._publish_files(
                    stored_task,
                    result.primary_path,
                    result.zip_path,
                )
                published_dir = primary_path.parent
                data = {
                    "conversation_id": result.conversation_id,
                    "primary_url": _file_url(primary_path, base_url),
                    "zip_url": _file_url(zip_path, base_url),
                }
            self._update_task(key, status=TASK_STATUS_SUCCESS, result=data, account_email=account_email, error="", ended_ts=time.time())
            success_recorded = True
            self._log_call(
                identity,
                kind,
                started,
                request_text(prompt),
                account_email=account_email,
                result=data,
                model=stored_task.get("model", EDITABLE_FILE_MODEL),
                endpoint="/v1/chat/completions" if transport == "chat" else "",
            )
        except Exception as exc:
            cleanup_paths = [staging_dir]
            if not success_recorded:
                cleanup_paths.append(published_dir)
            cleanup_succeeded = self._remove_directories(cleanup_paths)
            if kind == "file":
                if isinstance(exc, NativeFileError):
                    error = str(exc)
                elif isinstance(exc, EditableFileFailureError):
                    error = "native_file:generation_" + exc.failure.code
                else:
                    error = "native_file:task_" + type(exc).__name__
            else:
                error = str(exc) or "editable file task failed"
            if not cleanup_succeeded:
                error = f"{error}; generated files could not be removed"
            conversation_id = str(getattr(exc, "conversation_id", "") or "")
            recovery = {"conversation_id": conversation_id, "download_pending": True} if kind == "file" and conversation_id else {}
            self._update_task(key, status=TASK_STATUS_ERROR, error=error, account_email=account_email, ended_ts=time.time(), **({"result": recovery} if recovery else {}))
            self._log_call(
                identity,
                kind,
                started,
                request_text(prompt),
                status="failed",
                error=error,
                account_email=account_email,
                model=stored_task.get("model", EDITABLE_FILE_MODEL),
                endpoint="/v1/chat/completions" if transport == "chat" else "",
            )

    def public_file_path(self, relative_path: str) -> Path:
        value = str(relative_path or "")
        if "\\" in value or any(ord(character) < 32 for character in value):
            raise FileNotFoundError(value)
        raw = value.lstrip("/")
        root = EDITABLE_FILE_ROOT.resolve()
        requested = Path(raw)
        parts = requested.parts
        if len(parts) != 3:
            raise FileNotFoundError(raw)

        kind, storage_id, filename = parts
        allowed_extensions = EDITABLE_FILE_EXTENSIONS.get(kind)
        if (
            allowed_extensions is None
            or not _is_storage_id(storage_id)
            or not is_safe_public_filename(filename)
            or (kind != "file" and Path(filename).suffix.lower() not in allowed_extensions)
        ):
            raise FileNotFoundError(raw)

        try:
            path = (root / requested).resolve()
            path.relative_to(root)
            if not path.is_file():
                raise FileNotFoundError(raw)
        except (OSError, RuntimeError, ValueError) as exc:
            raise FileNotFoundError(raw) from exc
        return path

    def _publish_native_output(
        self,
        task: dict[str, Any],
        result: Any,
        *,
        base_url: str,
        output_bundle: str,
    ) -> dict[str, Any]:
        files = [Path(item) for item in (getattr(result, "files", []) or [])]
        text = clean_generated_text(getattr(result, "text", ""))
        if not files:
            if not text:
                raise NativeFileError("generation_no_actual_output")
            return {
                "conversation_id": getattr(result, "conversation_id", ""),
                "primary_url": "",
                "files": [],
                "original_files": [],
                "text": text,
                "delivery_format": "text_only",
            }
        staging = self._staging_dir(task)
        output = self._output_dir(task)
        if output.exists():
            raise NativeFileError("publish_output_exists")
        originals: list[dict[str, Any]] = []
        total = 0
        seen_names: set[str] = set()
        checked: list[Path] = []
        for path in files:
            resolved = path.resolve()
            if resolved.is_symlink() or resolved.parent != staging or not resolved.is_file():
                raise NativeFileError("publish_file_outside_staging")
            name = file_name(resolved.name)
            if name.casefold() in seen_names:
                raise NativeFileError("publish_duplicate_names")
            seen_names.add(name.casefold())
            data = resolved.read_bytes()
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise NativeFileError("output_total_size_limit")
            originals.append({
                "name": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            checked.append(resolved)
        unexpected = [
            item for item in staging.iterdir()
            if item.is_file() and item not in checked
        ]
        if unexpected:
            raise NativeFileError("publish_unexpected_files")
        if output_bundle == "zip" and len(checked) > 1:
            descriptor, temp_name = tempfile.mkstemp(prefix=".bundle-", suffix=".tmp", dir=staging)
            os.close(descriptor)
            bundle = Path(temp_name)
            try:
                with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                    for path in checked:
                        info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100600 << 16
                        archive.writestr(info, path.read_bytes())
                if bundle.stat().st_size > MAX_FILE_BYTES:
                    raise NativeFileError("bundle_oversize")
                with zipfile.ZipFile(bundle) as archive:
                    if archive.testzip() is not None:
                        raise NativeFileError("bundle_integrity_failed")
                for path in checked:
                    path.unlink()
                bundle.replace(staging / "files.zip")
                (staging / "files.zip").chmod(0o600)
            finally:
                bundle.unlink(missing_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.replace(output)
        published = sorted(output.iterdir(), key=lambda item: item.name)
        delivery_format = "zip" if len(published) == 1 and published[0].suffix.lower() == ".zip" else (
            "separate" if len(published) > 1 else "original"
        )
        return {
            "conversation_id": getattr(result, "conversation_id", ""),
            "primary_url": _file_url(published[0], base_url),
            "files": [
                {
                    "name": item.name,
                    "mime": mimetypes.guess_type(item.name)[0] or "application/octet-stream",
                    "size": item.stat().st_size,
                    "url": _file_url(item, base_url),
                }
                for item in published
            ],
            "original_files": originals,
            "text": text,
            "delivery_format": delivery_format,
        }

    def recover_native_output(
        self,
        identity: dict[str, object],
        task_id: str,
        *,
        base_url: str = "",
        output_bundle: str = "zip",
    ) -> dict[str, Any] | None:
        """GET-only recovery for a previously failed native file publication."""
        owner = _owner_id(identity)
        normalized_task_id = _clean(task_id)
        with self._lock:
            task = self._repository.get(owner, normalized_task_id)
        if task is None or task.get("kind") != "file":
            return None
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        conversation_id = _clean(result.get("conversation_id"))
        if not conversation_id or task.get("status") == TASK_STATUS_SUCCESS:
            return None
        model = _clean(task.get("model")) or EDITABLE_FILE_MODEL
        output_filename = _clean(task.get("output_filename")) or "result.txt"
        staging_dir = self._staging_dir(task)
        started_at = time.time()
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        try:
            token = _editable_access_token()
            with OpenAIBackendAPI(token) as backend:
                exported = backend.recover_file(
                    conversation_id,
                    staging_dir,
                    output_filename=output_filename,
                    timeout_secs=EDITABLE_FILE_TIMEOUT_SECS,
                )
            data = self._publish_native_output(
                task,
                exported,
                base_url=base_url,
                output_bundle=output_bundle,
            )
            self._update_task(
                normalized_task_id,
                status=TASK_STATUS_SUCCESS,
                result=data,
                error="",
                ended_ts=time.time(),
                updated_at=_now_iso(),
                updated_ts=time.time(),
            )
            self._log_call(
                identity,
                "file",
                started_at,
                "",
                result=data,
                model=model,
                endpoint="/v1/chat/completions",
            )
            return _public_task(
                self._repository.get(owner, normalized_task_id) or task
            )
        except Exception:
            self._remove_directories([staging_dir])
            raise

    def _publish_files(
            self,
            task: dict[str, Any],
            primary_path: Path,
            zip_path: Path,
    ) -> tuple[Path, Path]:
        staging_dir = self._staging_dir(task)
        output_dir = self._output_dir(task)
        primary = Path(primary_path).resolve()
        archive = Path(zip_path).resolve()
        kind = _clean(task.get("kind"))
        allowed_extensions = EDITABLE_FILE_EXTENSIONS[kind]
        if (
            primary.parent != staging_dir
            or archive.parent != staging_dir
            or not primary.is_file()
            or not archive.is_file()
            or not is_safe_public_filename(primary.name)
            or not is_safe_public_filename(archive.name)
            or primary.suffix.lower() not in (allowed_extensions - {".zip"})
            or archive.suffix.lower() != ".zip"
        ):
            raise RuntimeError("editable file export produced invalid artifacts")
        if output_dir.exists():
            raise RuntimeError("editable file output directory already exists")

        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir.replace(output_dir)
        return output_dir / primary.name, output_dir / archive.name

    @staticmethod
    def _output_dir(task: dict[str, Any]) -> Path:
        root = EDITABLE_FILE_ROOT.resolve()
        kind = _clean(task.get("kind"))
        if kind not in {"ppt", "psd", "file"}:
            raise ValueError("editable file task kind is invalid")
        storage_id = _storage_id(task)
        kind_root = (root / kind).resolve()
        output_dir = (kind_root / storage_id).resolve()
        kind_root.relative_to(root)
        output_dir.relative_to(root)
        if output_dir.parent != kind_root:
            raise ValueError("editable file task output directory is invalid")
        return output_dir

    @staticmethod
    def _staging_dir(task: dict[str, Any]) -> Path:
        root = EDITABLE_FILE_ROOT.resolve()
        kind = _clean(task.get("kind"))
        if kind not in EDITABLE_FILE_EXTENSIONS:
            raise ValueError("editable file task kind is invalid")
        storage_id = _storage_id(task)
        staging_root = (root / ".staging" / kind).resolve()
        staging_dir = (staging_root / storage_id).resolve()
        staging_root.relative_to(root)
        staging_dir.relative_to(staging_root)
        if staging_dir.parent != staging_root:
            raise ValueError("editable file task staging directory is invalid")
        return staging_dir

    def _remove_task_files(
            self,
            task: dict[str, Any],
            *,
            strict: bool = True,
    ) -> bool:
        directories: list[Path] = []
        for resolver in (self._staging_dir, self._output_dir):
            try:
                directories.append(resolver(task))
            except ValueError:
                continue
        succeeded = self._remove_directories(directories)
        if not succeeded and strict:
            raise EditableFileTaskCleanupError(
                "editable file task files could not be removed"
            )
        return succeeded

    @staticmethod
    def _remove_directories(directories: list[Path | None]) -> bool:
        succeeded = True
        for directory in dict.fromkeys(path for path in directories if path is not None):
            try:
                if directory.exists():
                    shutil.rmtree(directory)
                if directory.exists():
                    succeeded = False
            except OSError:
                succeeded = False
        return succeeded

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            owner_id, task_id = key.rsplit(":", 1)
            updates["updated_at"] = _now_iso()
            updates["updated_ts"] = time.time()
            self._repository.update(owner_id, task_id, **updates)

    def _recover_unfinished_locked(self) -> None:
        for task in self._repository.list_by_status(UNFINISHED_STATUSES):
            self._remove_task_files(task, strict=False)
            now = time.time()
            self._repository.update(
                _clean(task.get("owner_id")),
                _clean(task.get("id")),
                status=TASK_STATUS_ERROR,
                error="服务已重启，未完成的任务已中断",
                ended_ts=now,
                updated_at=_now_iso(),
                updated_ts=now,
            )

    def _recover_pending_deletions_locked(self) -> None:
        for task in self._repository.list_delete_pending():
            if self._remove_task_files(task, strict=False):
                self._repository.delete(
                    _clean(task.get("owner_id")),
                    _clean(task.get("id")),
                )

    def _log_call(
            self,
            identity: dict[str, object],
            kind: str,
            started: float,
            request_preview: str,
            *,
            status: str = "success",
            error: str = "",
            account_email: str = "",
            result: dict[str, str] | None = None,
            model: str = EDITABLE_FILE_MODEL,
            endpoint: str = "",
    ) -> None:
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint or f"/v1/{kind}/generations",
            "model": model,
            "started_at": beijing_from_timestamp(started),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if request_preview:
            detail["request_text"] = request_preview
        if account_email:
            detail["account_email"] = account_email
        if error:
            detail["error"] = error
        if result:
            detail["result"] = result
        try:
            log_service.add(LOG_TYPE_CALL, f"{kind.upper()}生成任务{'失败' if status == 'failed' else '完成'}", detail)
        except Exception:
            pass


editable_file_task_service = EditableFileTaskService()
