import argparse
import json
import os
import time
from pathlib import Path

import keyring
import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_CONFIG_PATH = Path(__file__).with_name("bank_robot.config.json")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_string(value: str, context: dict) -> str:
    result = value
    while "${" in result:
        start = result.index("${")
        end = result.index("}", start)
        expression = result[start + 2 : end]
        if expression.startswith("env:"):
            replacement = os.environ.get(expression[4:], "")
        else:
            replacement = str(context.get(expression, ""))
        result = result[:start] + replacement + result[end + 1 :]
    return result


def resolve_value(value, context: dict):
    if isinstance(value, str):
        return resolve_string(value, context)
    if isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, context) for key, item in value.items()}
    return value


def load_job_context(job: dict) -> dict:
    context = {
        "job_id": job["id"],
        "bank": job.get("bank", ""),
        "statement_kind": job.get("statement_kind", "account"),
        "ownership_category": job.get("ownership_category", "joint"),
        "institution": job.get("institution", job.get("bank", "")),
        "account_label": job.get("account_label", ""),
    }
    secrets_service = job.get("secrets_service") or f"inversiones_personales.bank_robot.{job['id']}"
    for secret_name in job.get("secret_fields", []):
        secret_value = keyring.get_password(secrets_service, secret_name)
        if secret_value is None:
            raise RuntimeError(
                f"No existe el secreto '{secret_name}' en Windows Credential Manager para el servicio '{secrets_service}'."
            )
        context[secret_name] = secret_value
    return context


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def execute_step(page, step: dict, download_dir: Path, context: dict, downloaded_files: list[Path]):
    action = step["action"]
    timeout = int(step.get("timeout_ms", 30000))

    if action == "goto":
        page.goto(step["url"], wait_until=step.get("wait_until", "load"), timeout=timeout)
        return
    if action == "wait_for":
        page.locator(step["selector"]).wait_for(state=step.get("state", "visible"), timeout=timeout)
        return
    if action == "click":
        page.locator(step["selector"]).click(timeout=timeout)
        return
    if action == "fill":
        page.locator(step["selector"]).fill(step["value"], timeout=timeout)
        return
    if action == "press":
        page.locator(step["selector"]).press(step["key"], timeout=timeout)
        return
    if action == "select":
        page.locator(step["selector"]).select_option(step["value"], timeout=timeout)
        return
    if action == "check":
        page.locator(step["selector"]).check(timeout=timeout)
        return
    if action == "uncheck":
        page.locator(step["selector"]).uncheck(timeout=timeout)
        return
    if action == "sleep":
        time.sleep(float(step.get("seconds", 1)))
        return
    if action == "manual_pause":
        message = step.get("message", "Completa el paso manual en el navegador y pulsa Enter para continuar.")
        input(f"{message}\n")
        return
    if action == "screenshot":
        screenshot_path = Path(step.get("path") or (download_dir / f"{context['job_id']}-debug.png"))
        ensure_directory(screenshot_path.parent)
        page.screenshot(path=str(screenshot_path), full_page=bool(step.get("full_page", True)))
        return
    if action == "download":
        ensure_directory(download_dir)
        with page.expect_download(timeout=timeout) as download_info:
            page.locator(step["selector"]).click(timeout=timeout)
        download = download_info.value
        suggested_name = step.get("filename") or download.suggested_filename
        destination = download_dir / suggested_name
        download.save_as(str(destination))
        downloaded_files.append(destination)
        print(f"[{context['job_id']}] Descargado: {destination}")
        return
    if action == "wait_for_download":
        ensure_directory(download_dir)
        message = step.get(
            "message",
            "Completa la descarga en la web del banco. El robot recogera el fichero automaticamente.",
        )
        print(f"[{context['job_id']}] {message}")
        download = page.wait_for_event("download", timeout=timeout)
        suggested_name = step.get("filename") or download.suggested_filename
        destination = download_dir / suggested_name
        download.save_as(str(destination))
        downloaded_files.append(destination)
        print(f"[{context['job_id']}] Descargado: {destination}")
        return
    raise RuntimeError(f"Accion de robot no soportada: {action}")


def upload_file(file_path: Path, job: dict, robot_config: dict, context: dict):
    upload_url = resolve_string(
        job.get("upload_url") or robot_config["server"]["upload_url"],
        context,
    )
    token_env = job.get("upload_token_env") or robot_config["server"].get("upload_token_env", "BANK_ROBOT_IMPORT_TOKEN")
    upload_token = os.environ.get(token_env, "").strip()
    if not upload_token:
        raise RuntimeError(f"No existe la variable de entorno {token_env} con el token de subida del robot.")

    verify_ssl = bool(robot_config.get("server", {}).get("verify_ssl", True))
    with file_path.open("rb") as handle:
        response = requests.post(
            upload_url,
            headers={"X-Bank-Robot-Token": upload_token},
            data={
                "statement_kind": context["statement_kind"],
                "ownership_category": context["ownership_category"],
                "institution": context["institution"],
                "account_label": context["account_label"],
            },
            files={"files": (file_path.name, handle, "application/octet-stream")},
            timeout=120,
            verify=verify_ssl,
        )
    if response.status_code not in {200, 207}:
        raise RuntimeError(f"Subida fallida ({response.status_code}): {response.text}")
    payload = response.json()
    if not payload.get("ok", True):
        raise RuntimeError(f"La API de importacion devolvio error: {payload}")
    print(f"[{context['job_id']}] Subido al servidor: {file_path.name}")


def run_job(job: dict, robot_config: dict, *, headless_override: bool | None, dry_run: bool, skip_upload: bool):
    context = load_job_context(job)
    context.update(
        {
            "base_url": job.get("base_url", ""),
        }
    )
    download_dir = Path(resolve_string(job.get("download_dir", str(Path("downloads") / job["id"])), context))
    storage_state_path = job.get("storage_state_path")
    browser_name = job.get("browser", "chromium")
    headless = headless_override if headless_override is not None else bool(job.get("headless", False))
    downloaded_files: list[Path] = []

    with sync_playwright() as playwright:
        browser_launcher = getattr(playwright, browser_name)
        browser = browser_launcher.launch(headless=headless)
        context_args = {
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 1024},
        }
        if storage_state_path and Path(storage_state_path).exists():
            context_args["storage_state"] = storage_state_path
        browser_context = browser.new_context(**context_args)
        page = browser_context.new_page()
        page.set_default_timeout(int(job.get("default_timeout_ms", 30000)))

        try:
            for raw_step in job.get("steps", []):
                step = resolve_value(raw_step, context)
                execute_step(page, step, download_dir, context, downloaded_files)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Timeout en el robot '{job['id']}': {exc}") from exc
        finally:
            if storage_state_path:
                ensure_directory(Path(storage_state_path).parent)
                browser_context.storage_state(path=storage_state_path)
            browser_context.close()
            browser.close()

    if not downloaded_files:
        print(f"[{context['job_id']}] No se ha descargado ningun fichero.")
        return []

    if dry_run or skip_upload:
        for downloaded in downloaded_files:
            print(f"[{context['job_id']}] Descarga lista en local: {downloaded}")
        return downloaded_files

    for downloaded in downloaded_files:
        upload_file(downloaded, job, robot_config, context)
    return downloaded_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Robot local de descarga bancaria por banco/cuenta/tarjeta.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Ruta al JSON de configuracion.")
    parser.add_argument("--job", action="append", help="ID de trabajo concreto. Se puede repetir.")
    parser.add_argument("--headed", action="store_true", help="Abre el navegador visible.")
    parser.add_argument("--headless", action="store_true", help="Fuerza modo sin interfaz.")
    parser.add_argument("--dry-run", action="store_true", help="Descarga pero no sube al servidor.")
    parser.add_argument("--skip-upload", action="store_true", help="No sube los ficheros descargados.")
    parser.add_argument("--list-jobs", action="store_true", help="Lista los trabajos configurados.")
    return parser.parse_args()


def main():
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        raise SystemExit(f"No existe el archivo de configuracion: {config_path}")

    robot_config = load_config(config_path)
    jobs = robot_config.get("jobs", [])
    if args.list_jobs:
        for job in jobs:
            print(f"{job['id']}: {job.get('description') or job.get('bank', '')}")
        return

    selected_jobs = jobs
    if args.job:
        selected_ids = set(args.job)
        selected_jobs = [job for job in jobs if job["id"] in selected_ids]
        missing_ids = selected_ids - {job["id"] for job in selected_jobs}
        if missing_ids:
            raise SystemExit(f"No existen estos jobs en la configuracion: {', '.join(sorted(missing_ids))}")

    if not selected_jobs:
        raise SystemExit("No hay trabajos configurados.")

    headless_override = None
    if args.headed:
        headless_override = False
    elif args.headless:
        headless_override = True

    failures = []
    for job in selected_jobs:
        print(f"[{job['id']}] Iniciando robot para {job.get('bank', job['id'])}...")
        try:
            run_job(
                job,
                robot_config,
                headless_override=headless_override,
                dry_run=args.dry_run,
                skip_upload=args.skip_upload,
            )
        except Exception as exc:
            failures.append((job["id"], str(exc)))
            print(f"[{job['id']}] ERROR: {exc}")

    if failures:
        for job_id, error in failures:
            print(f"- {job_id}: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
