import argparse
import json
import os
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from bank_robot import DEFAULT_CONFIG_PATH, ensure_directory, run_job as run_playwright_job


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WATCH_DIR = (Path.home() / "Downloads").expanduser()
DEFAULT_ALLOWED_ORIGINS = (
    "https://personal.neosaware.ai",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8082",
    "http://localhost:8082",
)
LEGACY_MANUAL_DOWNLOAD_ACTIONS = {
    "goto",
    "wait_for_download",
    "manual_pause",
    "sleep",
    "screenshot",
}


def infer_login_url(job: dict) -> str:
    if job.get("login_url"):
        return str(job.get("login_url", "")).strip()
    for step in job.get("steps", []):
        if isinstance(step, dict) and step.get("action") == "goto" and step.get("url"):
            return str(step.get("url", "")).strip()
    return ""


def looks_like_legacy_manual_download_job(job: dict) -> bool:
    if job.get("mode"):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    actions = {str(step.get("action", "")).strip() for step in steps if isinstance(step, dict)}
    if "wait_for_download" not in actions:
        return False
    if job.get("secret_fields"):
        return False
    return actions.issubset(LEGACY_MANUAL_DOWNLOAD_ACTIONS)


def infer_watch_dir(job: dict) -> str:
    explicit_watch_dir = str(job.get("watch_dir", "")).strip()
    if explicit_watch_dir:
        return explicit_watch_dir

    download_dir = str(job.get("download_dir", "")).strip()
    if download_dir:
        resolved_download_dir = Path(download_dir).expanduser()
        try:
            if DEFAULT_WATCH_DIR == resolved_download_dir or DEFAULT_WATCH_DIR in resolved_download_dir.parents:
                return str(DEFAULT_WATCH_DIR)
        except Exception:
            pass
        return str(resolved_download_dir)

    return str(DEFAULT_WATCH_DIR)


def normalize_job(job: dict) -> dict:
    normalized_job = dict(job)
    inferred_mode = "manual_download" if looks_like_legacy_manual_download_job(normalized_job) else ""
    mode = str(normalized_job.get("mode") or inferred_mode or "playwright").strip() or "playwright"
    normalized_job["mode"] = mode
    if mode == "manual_download":
        normalized_job["login_url"] = infer_login_url(normalized_job)
        normalized_job["watch_dir"] = infer_watch_dir(normalized_job)
        normalized_job.setdefault("allowed_extensions", [".xls", ".xlsx"])
        normalized_job.setdefault("manual_timeout_seconds", 900)
        normalized_job.setdefault("manual_poll_interval_seconds", 1.0)
    return normalized_job


def normalize_bridge_config(config: dict) -> dict:
    normalized = dict(config)
    normalized["server"] = normalized.get("server", {}) if isinstance(normalized.get("server"), dict) else {}
    jobs = normalized.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []
    normalized["jobs"] = [normalize_job(job) for job in jobs if isinstance(job, dict)]
    return normalized


def load_bridge_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {"server": {}, "jobs": []}
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return normalize_bridge_config(data)


def save_bridge_config(config_path: Path, config: dict) -> None:
    ensure_directory(config_path.parent)
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def summarize_job(job: dict) -> dict:
    job = normalize_job(job)
    login_url = infer_login_url(job)
    return {
        "id": job.get("id", ""),
        "description": job.get("description", ""),
        "bank": job.get("bank", ""),
        "institution": job.get("institution", ""),
        "account_label": job.get("account_label", ""),
        "statement_kind": job.get("statement_kind", "account"),
        "ownership_category": job.get("ownership_category", "joint"),
        "mode": job.get("mode", "playwright"),
        "login_url": login_url,
        "watch_dir": job.get("watch_dir", ""),
    }


def validate_job_payload(job: dict) -> dict:
    normalized_job = normalize_job(job)
    mode = str(normalized_job.get("mode", "playwright")).strip() or "playwright"
    common_required_fields = {
        "id",
        "description",
        "bank",
        "institution",
        "account_label",
        "statement_kind",
        "ownership_category",
    }
    if mode == "manual_download":
        required_fields = common_required_fields | {"watch_dir"}
    else:
        required_fields = common_required_fields | {"download_dir", "storage_state_path", "steps"}
    missing = sorted(field for field in required_fields if not normalized_job.get(field))
    if missing:
        raise ValueError(f"Faltan campos obligatorios del trabajo: {', '.join(missing)}")
    if mode != "manual_download" and not isinstance(normalized_job["steps"], list):
        raise ValueError("steps debe ser una lista.")
    return normalized_job


def snapshot_directory(directory: Path, extensions: set[str]) -> dict[str, tuple[float, int]]:
    snapshot = {}
    if not directory.exists():
        return snapshot
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        stat_result = path.stat()
        snapshot[str(path)] = (stat_result.st_mtime, stat_result.st_size)
    return snapshot


def run_manual_download_job(job: dict, *, open_url: bool = True) -> list[Path]:
    watch_dir = Path(job.get("watch_dir", str(DEFAULT_WATCH_DIR))).expanduser().resolve()
    ensure_directory(watch_dir)
    allowed_extensions = {
        extension if str(extension).startswith(".") else f".{extension}"
        for extension in job.get("allowed_extensions", [".xls", ".xlsx"])
    }
    before_snapshot = snapshot_directory(watch_dir, allowed_extensions)
    login_url = str(job.get("login_url", "")).strip()
    if open_url and login_url and login_url != "about:blank":
        webbrowser.open(login_url, new=2, autoraise=True)
    timeout_seconds = int(job.get("manual_timeout_seconds", 900))
    poll_interval = float(job.get("manual_poll_interval_seconds", 1.0))
    deadline = time.time() + timeout_seconds

    print(f"[{job['id']}] Esperando una descarga manual en {watch_dir}...")
    while time.time() < deadline:
        after_snapshot = snapshot_directory(watch_dir, allowed_extensions)
        changed_files = []
        for path_str, details in after_snapshot.items():
            if before_snapshot.get(path_str) != details:
                changed_files.append((Path(path_str), details[0]))
        if changed_files:
            changed_files.sort(key=lambda item: item[1], reverse=True)
            selected_file = changed_files[0][0]
            print(f"[{job['id']}] Detectado fichero descargado: {selected_file}")
            return [selected_file]
        time.sleep(poll_interval)

    raise RuntimeError(
        f"No se ha detectado ningun XLS/XLSX nuevo en {watch_dir}. Descarga el extracto desde tu navegador normal y vuelve a intentarlo."
    )


@dataclass
class RobotBridgeState:
    config_path: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def status_payload(self) -> dict:
        config = load_bridge_config(self.config_path)
        with self.lock:
            runs = list(self.last_runs.values())
        recent_runs = sorted(runs, key=lambda item: item["finished_at"], reverse=True)[:10]
        return {
            "ok": True,
            "config_path": str(self.config_path),
            "jobs": [summarize_job(job) for job in config.get("jobs", [])],
            "recent_runs": recent_runs,
        }

    def upsert_job(self, job: dict) -> dict:
        job = validate_job_payload(job)
        with self.lock:
            config = load_bridge_config(self.config_path)
            jobs = config.setdefault("jobs", [])
            updated = False
            for index, current_job in enumerate(jobs):
                if current_job.get("id") == job["id"]:
                    jobs[index] = job
                    updated = True
                    break
            if not updated:
                jobs.append(job)
            save_bridge_config(self.config_path, config)
        return summarize_job(job)

    def delete_job(self, job_id: str) -> None:
        with self.lock:
            config = load_bridge_config(self.config_path)
            remaining_jobs = [job for job in config.get("jobs", []) if job.get("id") != job_id]
            if len(remaining_jobs) == len(config.get("jobs", [])):
                raise KeyError(job_id)
            config["jobs"] = remaining_jobs
            save_bridge_config(self.config_path, config)

    def run_job(self, job_id: str, *, headed: bool = True, open_url: bool = True) -> dict:
        config = load_bridge_config(self.config_path)
        job = next((item for item in config.get("jobs", []) if item.get("id") == job_id), None)
        if not job:
            raise KeyError(job_id)

        run_id = uuid4().hex
        if job.get("mode") == "manual_download":
            downloaded_files = run_manual_download_job(job, open_url=open_url)
        else:
            downloaded_files = run_playwright_job(
                job,
                config,
                headless_override=(False if headed else True),
                dry_run=False,
                skip_upload=True,
            )
        files = []
        for index, file_path in enumerate(downloaded_files):
            file_item = {
                "index": index,
                "name": file_path.name,
                "size": file_path.stat().st_size,
                "statement_kind": job.get("statement_kind", "account"),
                "ownership_category": job.get("ownership_category", "joint"),
                "institution": job.get("institution", ""),
                "account_label": job.get("account_label", ""),
            }
            files.append(file_item)

        run_info = {
            "run_id": run_id,
            "job": summarize_job(job),
            "files": files,
            "file_paths": [str(path) for path in downloaded_files],
            "finished_at": time.time(),
        }
        with self.lock:
            self.last_runs[run_id] = run_info
        return {
            "ok": True,
            "run_id": run_id,
            "job": summarize_job(job),
            "files": files,
        }

    def get_run_file(self, run_id: str, index: int) -> Path:
        with self.lock:
            run_info = self.last_runs.get(run_id)
        if not run_info:
            raise KeyError(run_id)
        file_paths = run_info.get("file_paths", [])
        if index < 0 or index >= len(file_paths):
            raise IndexError(index)
        file_path = Path(file_paths[index])
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        return file_path


class RobotBridgeHandler(BaseHTTPRequestHandler):
    server_version = "BankRobotBridge/1.0"

    @property
    def state(self) -> RobotBridgeState:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return self.server.allowed_origins  # type: ignore[attr-defined]

    def _origin_allowed(self) -> str | None:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return None
        if origin in self.allowed_origins:
            return origin
        return None

    def _set_cors_headers(self) -> None:
        allowed_origin = self._origin_allowed()
        if allowed_origin:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            return self._send_json(self.state.status_payload())
        if parsed.path.startswith("/runs/") and "/files/" in parsed.path:
            return self._send_run_file(parsed.path)
        return self._send_json({"ok": False, "error": "Ruta no encontrada."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        try:
            if parsed.path == "/jobs/upsert":
                job_summary = self.state.upsert_job(payload["job"])
                return self._send_json({"ok": True, "job": job_summary})
            if parsed.path == "/jobs/delete":
                self.state.delete_job(str(payload.get("job_id", "")))
                return self._send_json({"ok": True})
            if parsed.path == "/run":
                result = self.state.run_job(
                    str(payload.get("job_id", "")),
                    headed=bool(payload.get("headed", True)),
                    open_url=bool(payload.get("open_url", True)),
                )
                return self._send_json(result)
        except KeyError as exc:
            return self._send_json(
                {"ok": False, "error": f"No existe el elemento solicitado: {exc}"},
                status=HTTPStatus.NOT_FOUND,
            )
        except (ValueError, IndexError, FileNotFoundError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - fallback defensivo
            return self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        return self._send_json({"ok": False, "error": "Ruta no encontrada."}, status=HTTPStatus.NOT_FOUND)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            return {}
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("El cuerpo de la peticion debe ser JSON valido.") from exc
        if not isinstance(payload, dict):
            raise ValueError("El cuerpo de la peticion debe ser un objeto JSON.")
        return payload

    def _send_json(self, payload: dict, *, status: int = HTTPStatus.OK) -> None:
        response = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(response)

    def _send_run_file(self, path: str) -> None:
        parts = [segment for segment in path.split("/") if segment]
        if len(parts) != 4 or parts[0] != "runs" or parts[2] != "files":
            return self._send_json({"ok": False, "error": "Ruta de fichero no valida."}, status=HTTPStatus.NOT_FOUND)
        run_id = parts[1]
        try:
            index = int(parts[3])
            file_path = self.state.get_run_file(run_id, index)
        except (ValueError, KeyError, IndexError, FileNotFoundError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.NOT_FOUND)

        payload = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        print(format % args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Asistente local para lanzar el robot bancario desde la web.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host local donde escuchar.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Puerto local del asistente.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Ruta al JSON de configuracion local.")
    parser.add_argument(
        "--allowed-origins",
        default=os.environ.get("BANK_ROBOT_BRIDGE_ALLOWED_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)),
        help="Lista separada por comas de orígenes web autorizados.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    ensure_directory(config_path.parent)
    if not config_path.exists():
        save_bridge_config(config_path, {"server": {}, "jobs": []})
    else:
        save_bridge_config(config_path, load_bridge_config(config_path))
    allowed_origins = tuple(origin.strip() for origin in args.allowed_origins.split(",") if origin.strip())
    server = ThreadingHTTPServer((args.host, args.port), RobotBridgeHandler)
    server.state = RobotBridgeState(config_path=config_path)  # type: ignore[attr-defined]
    server.allowed_origins = allowed_origins  # type: ignore[attr-defined]
    print(f"Asistente bancario local escuchando en http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
