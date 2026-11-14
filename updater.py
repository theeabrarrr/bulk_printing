import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

import config_manager
import logger_manager
import version

logger = logger_manager.get_logger("updater")

STAGING_DIR_NAME = "_update_staging"
ROLLBACK_DIR_NAME = "_update_rollback"
UPDATE_BAT_NAME = "_run_update.bat"

# Files and directories in the suite root that must NEVER be overwritten by an update
PROTECTED_RUNTIME_ITEMS = {
    "config.json",
    "complaints.xlsx",
    "complaints_state.db",
    "complaints_state.db-wal",
    "complaints_state.db-shm",
    "completed_complaints.txt",
    "printed_complaints.txt",
    "today_complaints",
    "logs",
}


def is_frozen_executable() -> bool:
    """Returns True when running from the packaged PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def cleanup_update_artifacts(base_dir=None):
    """
    Removes leftover temporary update staging, rollback folders, or helper batch script
    from a prior update cycle. Safe to call on every application startup.
    """
    if base_dir is None:
        base_dir = config_manager.get_base_dir()

    for folder_name in (STAGING_DIR_NAME, ROLLBACK_DIR_NAME):
        folder_path = os.path.join(base_dir, folder_name)
        if os.path.exists(folder_path):
            try:
                shutil.rmtree(folder_path, ignore_errors=True)
                logger.debug("Cleaned up leftover update directory: %s", folder_path)
            except Exception as e:
                logger.debug("Could not remove leftover directory %s: %s", folder_path, e)

    bat_path = os.path.join(base_dir, UPDATE_BAT_NAME)
    if os.path.exists(bat_path):
        try:
            os.remove(bat_path)
            logger.debug("Cleaned up leftover update script: %s", bat_path)
        except Exception as e:
            logger.debug("Could not remove leftover script %s: %s", bat_path, e)


def _build_github_headers() -> dict:
    """Returns standard HTTP headers for GitHub API and asset requests."""
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{version.APP_NAME}/{version.__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _select_release_assets(assets: list, expected_zip_name: str):
    """
    Inspects the GitHub Release assets array and selects the binary ZIP asset
    and optional .sha256 sidecar asset.
    """
    zip_asset = None
    sha256_asset = None

    if not isinstance(assets, list):
        return None, None

    # 1. Exact match for canonical ZIP name first, then fallback to any .zip asset
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if name.lower() == expected_zip_name.lower():
            zip_asset = asset
            break

    if zip_asset is None:
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            if name.lower().endswith(".zip") and version.APP_NAME.lower() in name.lower():
                zip_asset = asset
                break

    if zip_asset is None:
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").strip()
            if name.lower().endswith(".zip"):
                zip_asset = asset
                break

    # 2. Match .sha256 sidecar asset
    target_sha_name = f"{zip_asset.get('name')}.sha256".lower() if zip_asset else f"{expected_zip_name}.sha256".lower()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if name.lower() == target_sha_name or name.lower().endswith(".sha256"):
            sha256_asset = asset
            break

    return zip_asset, sha256_asset


def check_for_updates(config=None, timeout=None, current_version=None) -> dict:
    """
    Queries the public GitHub Releases API (/releases/latest) and checks if a newer
    version of ERP_Automation_Suite is available.

    Never raises exceptions; network/API errors are captured in result["error"].
    """
    updater_cfg = config_manager.get_updater_settings(config)
    repo = updater_cfg.get("github_repo", version.GITHUB_REPO)
    if timeout is None:
        timeout = updater_cfg.get("startup_timeout_seconds", 3.0)
    if current_version is None:
        current_version = version.__version__

    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    result = {
        "checked": False,
        "update_available": False,
        "current_version": version.normalize_version_string(current_version),
        "latest_version": None,
        "tag_name": None,
        "release_name": None,
        "release_notes": "",
        "html_url": f"https://github.com/{repo}/releases",
        "published_at": None,
        "zip_name": None,
        "zip_url": None,
        "zip_size": 0,
        "sha256_url": None,
        "expected_sha256": "",
        "error": None,
    }

    req = urllib.request.Request(api_url, headers=_build_github_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw_body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result["error"] = f"No published releases found at {repo}."
        elif e.code == 403:
            result["error"] = "GitHub API rate limit exceeded. Try again later."
        else:
            result["error"] = f"GitHub API HTTP {e.code}: {e.reason}"
        logger.debug("Update check HTTP error (%s): %s", api_url, result["error"])
        return result
    except urllib.error.URLError as e:
        result["error"] = f"Network unreachable or timed out: {e.reason}"
        logger.debug("Update check network error (%s): %s", api_url, result["error"])
        return result
    except Exception as e:
        result["error"] = f"Unexpected error checking for updates: {e}"
        logger.debug("Update check error (%s): %s", api_url, result["error"])
        return result

    if not isinstance(data, dict):
        result["error"] = "Invalid response payload from GitHub Releases API."
        return result

    tag_name = str(data.get("tag_name") or "").strip()
    if not tag_name:
        result["error"] = "Latest GitHub release does not contain a valid tag_name."
        return result

    latest_norm = version.normalize_version_string(tag_name)
    expected_zip = version.get_release_zip_name(latest_norm)
    zip_asset, sha256_asset = _select_release_assets(data.get("assets", []), expected_zip)

    result["checked"] = True
    result["tag_name"] = tag_name
    result["latest_version"] = latest_norm
    result["release_name"] = str(data.get("name") or tag_name).strip()
    result["release_notes"] = str(data.get("body") or "").strip()
    result["html_url"] = str(data.get("html_url") or result["html_url"]).strip()
    result["published_at"] = data.get("published_at")

    if zip_asset:
        result["zip_name"] = zip_asset.get("name")
        result["zip_url"] = zip_asset.get("browser_download_url")
        try:
            result["zip_size"] = int(zip_asset.get("size") or 0)
        except (ValueError, TypeError):
            result["zip_size"] = 0

        # Check native GitHub asset digest ("sha256:...")
        digest_str = str(zip_asset.get("digest") or "")
        extracted_digest = version.extract_sha256_hex(digest_str)
        if extracted_digest:
            result["expected_sha256"] = extracted_digest

    if sha256_asset:
        result["sha256_url"] = sha256_asset.get("browser_download_url")

    # Fallback: extract SHA-256 from release notes if not in asset digest
    if not result["expected_sha256"] and result["release_notes"]:
        body_hash = version.extract_sha256_hex(result["release_notes"])
        if body_hash:
            result["expected_sha256"] = body_hash

    result["update_available"] = version.is_newer_version(latest_norm, current_version)
    return result


def compute_file_sha256(file_path: str) -> str:
    """Computes lowercase hexadecimal SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def _fetch_remote_sha256(sha256_url: str, timeout: float = 15.0) -> str:
    """Downloads a .sha256 sidecar file and extracts the 64-character hex SHA-256 digest."""
    if not sha256_url:
        return ""
    req = urllib.request.Request(
        sha256_url,
        headers={"User-Agent": f"{version.APP_NAME}/{version.__version__}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    return version.extract_sha256_hex(text)


def _safe_extract_zip(zip_path: str, extract_to: str):
    """
    Safely extracts a ZIP archive into extract_to, guarding against path traversal (Zip Slip).
    """
    os.makedirs(extract_to, exist_ok=True)
    abs_target_root = os.path.abspath(extract_to)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_dest = os.path.abspath(os.path.join(abs_target_root, member.filename))
            if not (
                member_dest == abs_target_root
                or member_dest.startswith(abs_target_root + os.sep)
            ):
                raise RuntimeError(
                    f"Unsafe path detected inside update archive: '{member.filename}'"
                )
        zf.extractall(abs_target_root)


def locate_staged_suite_root(extracted_dir: str) -> str:
    """
    Locates the folder containing ERP_Automation_Suite.exe and _internal/ inside extracted_dir.
    Supports both flat archives and archives nested under ERP_Automation_Suite/.
    Also strips any protected runtime files if accidentally present in the archive.
    """
    exe_filename = f"{version.EXE_NAME}.exe"

    candidates = [
        extracted_dir,
        os.path.join(extracted_dir, version.APP_NAME),
    ]
    # Also check if there is a single top-level directory inside extracted_dir
    try:
        entries = [
            os.path.join(extracted_dir, e)
            for e in os.listdir(extracted_dir)
            if os.path.isdir(os.path.join(extracted_dir, e))
        ]
        for entry in entries:
            if entry not in candidates:
                candidates.append(entry)
    except Exception:
        pass

    matched_root = None
    for candidate in candidates:
        cand_exe = os.path.join(candidate, exe_filename)
        cand_internal = os.path.join(candidate, "_internal")
        if os.path.isfile(cand_exe) and os.path.isdir(cand_internal):
            matched_root = candidate
            break

    if not matched_root:
        raise FileNotFoundError(
            f"Staged update archive is invalid: missing '{exe_filename}' or '_internal/' directory."
        )

    # Guarantee no runtime state files inside staged_root can ever overwrite user data
    for entry in list(os.listdir(matched_root)):
        low = entry.lower()
        if (
            low in PROTECTED_RUNTIME_ITEMS
            or low.endswith(".bak")
            or low.endswith(".db")
            or low.endswith(".db-wal")
            or low.endswith(".db-shm")
        ):
            target = os.path.join(matched_root, entry)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.remove(target)
                except OSError:
                    pass

    return matched_root


def download_and_stage_update(
    update_info: dict,
    config=None,
    base_dir=None,
    progress_callback=None,
) -> str:
    """
    Downloads the release ZIP into <base_dir>/_update_staging/, verifies its SHA-256
    checksum, extracts it, and validates the staged binary layout.

    Returns the absolute path to the validated staged suite directory containing
    ERP_Automation_Suite.exe and _internal/.
    """
    if not isinstance(update_info, dict) or not update_info.get("zip_url"):
        raise ValueError("No downloadable release ZIP asset URL found in update metadata.")

    if base_dir is None:
        base_dir = config_manager.get_base_dir()

    updater_cfg = config_manager.get_updater_settings(config)
    dl_timeout = updater_cfg.get("download_timeout_seconds", 60.0)

    staging_root = os.path.join(base_dir, STAGING_DIR_NAME)
    if os.path.exists(staging_root):
        shutil.rmtree(staging_root, ignore_errors=True)
    os.makedirs(staging_root, exist_ok=True)

    zip_name = update_info.get("zip_name") or version.get_release_zip_name(
        update_info.get("latest_version") or version.__version__
    )
    zip_path = os.path.join(staging_root, os.path.basename(zip_name))
    extracted_dir = os.path.join(staging_root, "extracted")

    try:
        # 1. Resolve expected SHA-256 (from .sha256 sidecar URL if not already known)
        expected_sha256 = str(update_info.get("expected_sha256") or "").strip().lower()
        if not expected_sha256 and update_info.get("sha256_url"):
            try:
                expected_sha256 = _fetch_remote_sha256(
                    update_info["sha256_url"], timeout=min(15.0, dl_timeout)
                )
            except Exception as sha_err:
                logger.warning("Could not fetch .sha256 sidecar file: %s", sha_err)

        # 2. Download the release ZIP archive
        req = urllib.request.Request(
            update_info["zip_url"],
            headers={"User-Agent": f"{version.APP_NAME}/{version.__version__}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=float(dl_timeout)) as resp:
            total_bytes = int(resp.headers.get("Content-Length") or update_info.get("zip_size") or 0)
            downloaded = 0
            with open(zip_path, "wb") as out_f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_bytes)

        if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
            raise RuntimeError("Downloaded update archive is empty or missing.")

        # 3. Verify SHA-256 integrity
        actual_sha256 = compute_file_sha256(zip_path)
        if expected_sha256:
            if actual_sha256 != expected_sha256:
                raise ValueError(
                    f"SHA-256 checksum mismatch! Expected {expected_sha256}, got {actual_sha256}."
                )
            logger.info("  [+] SHA-256 checksum verified: %s", actual_sha256)
        else:
            logger.info("  [i] Archive SHA-256 computed: %s (no remote checksum published)", actual_sha256)

        # 4. Extract and validate layout
        _safe_extract_zip(zip_path, extracted_dir)
        staged_suite_dir = locate_staged_suite_root(extracted_dir)
        return staged_suite_dir

    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def generate_update_batch_script(
    base_dir: str,
    staged_suite_dir: str,
    target_pid: int = None,
    relaunch: bool = True,
    target_version: str = "",
) -> str:
    """
    Generates the detached Windows batch helper (_run_update.bat) in base_dir.
    Uses `tasklist /FI "PID eq <PID>"` (never wmic.exe, which is removed in Windows 11 24H2+)
    to wait for the main process to exit, swaps ERP_Automation_Suite.exe and _internal/
    with automatic rollback protection, cleans up staging/rollback folders, and relaunches.
    """
    if target_pid is None:
        target_pid = os.getpid()

    base_dir = os.path.abspath(base_dir)
    staged_suite_dir = os.path.abspath(staged_suite_dir)
    staging_root = os.path.join(base_dir, STAGING_DIR_NAME)
    rollback_dir = os.path.join(base_dir, ROLLBACK_DIR_NAME)
    bat_path = os.path.join(base_dir, UPDATE_BAT_NAME)
    exe_name = f"{version.EXE_NAME}.exe"
    ver_label = f" to v{target_version}" if target_version else ""

    relaunch_cmd = (
        f'echo [*] Relaunching {exe_name}...\r\n'
        f'start "" "%BASE_DIR%\\{exe_name}"\r\n'
        if relaunch
        else f'echo [+] Update complete (relaunch skipped).\r\n'
    )

    script_lines = [
        "@echo off",
        "setlocal EnableExtensions",
        f'title Updating {version.APP_NAME}{ver_label}...',
        f'set "BASE_DIR={base_dir}"',
        f'set "STAGED_DIR={staged_suite_dir}"',
        f'set "STAGING_ROOT={staging_root}"',
        f'set "ROLLBACK_DIR={rollback_dir}"',
        f'set "EXE_NAME={exe_name}"',
        f'set "TARGET_PID={int(target_pid)}"',
        "",
        "echo ============================================================",
        f"echo   UPDATING {version.APP_NAME}{ver_label}",
        "echo ============================================================",
        "echo [*] Waiting for application (PID %TARGET_PID%) to close...",
        "",
        "set /A WAIT_COUNT=0",
        ":WAIT_LOOP",
        'tasklist /FI "PID eq %TARGET_PID%" /NH 2>nul | findstr /I /C:"%TARGET_PID%" >nul',
        "if errorlevel 1 goto PROCESS_EXITED",
        "set /A WAIT_COUNT+=1",
        "if %WAIT_COUNT% GEQ 15 (",
        "    echo [!] Application still running after 15s; forcing termination...",
        "    taskkill /F /PID %TARGET_PID% >nul 2>&1",
        "    timeout /t 1 /nobreak >nul 2>&1",
        "    goto PROCESS_EXITED",
        ")",
        "timeout /t 1 /nobreak >nul 2>&1",
        "goto WAIT_LOOP",
        "",
        ":PROCESS_EXITED",
        "echo [*] Preparing rollback backup of current binaries...",
        'if exist "%ROLLBACK_DIR%" rmdir /S /Q "%ROLLBACK_DIR%" >nul 2>&1',
        'mkdir "%ROLLBACK_DIR%" >nul 2>&1',
        "",
        'if exist "%BASE_DIR%\\%EXE_NAME%" (',
        '    move /Y "%BASE_DIR%\\%EXE_NAME%" "%ROLLBACK_DIR%\\%EXE_NAME%" >nul',
        "    if errorlevel 1 goto ROLLBACK",
        ")",
        'if exist "%BASE_DIR%\\_internal" (',
        '    move /Y "%BASE_DIR%\\_internal" "%ROLLBACK_DIR%\\_internal" >nul',
        "    if errorlevel 1 goto ROLLBACK",
        ")",
        "",
        "echo [*] Installing new application binaries...",
        'move /Y "%STAGED_DIR%\\%EXE_NAME%" "%BASE_DIR%\\%EXE_NAME%" >nul',
        "if errorlevel 1 goto ROLLBACK",
        'move /Y "%STAGED_DIR%\\_internal" "%BASE_DIR%\\_internal" >nul',
        "if errorlevel 1 goto ROLLBACK",
        "",
        'if not exist "%BASE_DIR%\\%EXE_NAME%" goto ROLLBACK',
        'if not exist "%BASE_DIR%\\_internal" goto ROLLBACK',
        "",
        "echo [*] Cleaning up temporary update files...",
        'if exist "%STAGING_ROOT%" rmdir /S /Q "%STAGING_ROOT%" >nul 2>&1',
        'if exist "%ROLLBACK_DIR%" rmdir /S /Q "%ROLLBACK_DIR%" >nul 2>&1',
        "echo [+] Update installed successfully!",
        relaunch_cmd.rstrip("\r\n"),
        '(goto) 2>nul & del /F /Q "%~f0"',
        "",
        ":ROLLBACK",
        "echo [!] ERROR: File swap failed! Restoring previous version from rollback backup...",
        'if exist "%BASE_DIR%\\%EXE_NAME%" del /F /Q "%BASE_DIR%\\%EXE_NAME%" >nul 2>&1',
        'if exist "%BASE_DIR%\\_internal" rmdir /S /Q "%BASE_DIR%\\_internal" >nul 2>&1',
        'if exist "%ROLLBACK_DIR%\\%EXE_NAME%" move /Y "%ROLLBACK_DIR%\\%EXE_NAME%" "%BASE_DIR%\\%EXE_NAME%" >nul 2>&1',
        'if exist "%ROLLBACK_DIR%\\_internal" move /Y "%ROLLBACK_DIR%\\_internal" "%BASE_DIR%\\_internal" >nul 2>&1',
        'if exist "%STAGING_ROOT%" rmdir /S /Q "%STAGING_ROOT%" >nul 2>&1',
        'if exist "%ROLLBACK_DIR%" rmdir /S /Q "%ROLLBACK_DIR%" >nul 2>&1',
        "echo [+] Previous version restored safely.",
        "exit /b 1",
        "",
    ]

    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(script_lines))

    return bat_path


def launch_detached_updater_and_exit(bat_path: str):
    """
    Closes open rotating log file handlers so logs/ is unlocked, spawns the detached
    batch updater in a new console window, and exits the current process immediately.
    """
    base_dir = os.path.dirname(os.path.abspath(bat_path))
    logger.info("Spawning detached update helper: %s", bat_path)
    logger_manager.close_logging_handlers()

    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        cwd=base_dir,
        creationflags=creationflags,
        close_fds=True,
    )
    sys.exit(0)


def _print_progress_bar(downloaded: int, total: int):
    """Renders an in-place console download progress bar."""
    if total > 0:
        pct = min(100.0, (downloaded / total) * 100.0)
        filled = int(pct // 4)
        bar = "=" * filled + (" " * (25 - filled))
        mb_done = downloaded / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        sys.stdout.write(f"\r  Downloading: [{bar}] {pct:5.1f}% ({mb_done:.1f} / {mb_total:.1f} MB)")
    else:
        mb_done = downloaded / (1024 * 1024)
        sys.stdout.write(f"\r  Downloading: {mb_done:.1f} MB...")
    sys.stdout.flush()


def _format_release_notes_preview(notes: str, max_lines: int = 8) -> str:
    """Formats a concise multi-line preview of the release notes for the CLI prompt."""
    if not notes or not notes.strip():
        return "    (No release notes provided)"
    lines = [line.strip() for line in notes.strip().splitlines() if line.strip()]
    if not lines:
        return "    (No release notes provided)"
    preview = [f"    {line}" for line in lines[:max_lines]]
    if len(lines) > max_lines:
        preview.append(f"    ... (+{len(lines) - max_lines} more lines)")
    return "\n".join(preview)


def perform_self_update(update_info: dict, config=None) -> bool:
    """
    Executes the full download, SHA-256 verification, staging, and detached swap launch.
    Guards against overwriting raw Python source files when running unfrozen.
    """
    if not is_frozen_executable():
        print("\n" + "!" * 65)
        print(" [DEV MODE] Running from unfrozen Python source (python main.py).")
        print(" Binary self-update is only active inside packaged ERP_Automation_Suite.exe")
        print(" to prevent overwriting your development source tree.")
        print(" To update your source workspace, run: git pull")
        print("!" * 65)
        return False

    if not update_info.get("zip_url"):
        logger.error(
            "\n[!] Release v%s does not have a packaged ZIP asset attached yet.\n"
            "    Visit: %s",
            update_info.get("latest_version"),
            update_info.get("html_url"),
        )
        return False

    latest_ver = update_info.get("latest_version")
    print(f"\n[*] Downloading ERP_Automation_Suite v{latest_ver}...")
    try:
        staged_suite_dir = download_and_stage_update(
            update_info,
            config=config,
            progress_callback=_print_progress_bar,
        )
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write("\n")
        sys.stdout.flush()
        logger.error("\n[!] Update download or verification failed: %s", e)
        print("    Your current installation and local data were not modified.")
        return False

    try:
        base_dir = config_manager.get_base_dir()
        bat_path = generate_update_batch_script(
            base_dir=base_dir,
            staged_suite_dir=staged_suite_dir,
            target_pid=os.getpid(),
            relaunch=True,
            target_version=latest_ver,
        )
        print(f"  [+] Update v{latest_ver} staged and verified.")
        print("  [*] Closing application to apply update and restart automatically...")
        launch_detached_updater_and_exit(bat_path)
        return True
    except SystemExit:
        raise
    except Exception as e:
        logger.error("\n[!] Failed to launch detached update helper: %s", e)
        cleanup_update_artifacts()
        return False


def prompt_and_install_update(update_info: dict, config=None) -> bool:
    """
    Displays update details and prompts the operator whether to install now.
    Returns True if an update was initiated, False if skipped/declined.
    """
    curr_v = update_info.get("current_version", version.__version__)
    latest_v = update_info.get("latest_version", "?")
    size_mb = (update_info.get("zip_size") or 0) / (1024 * 1024)
    size_label = f" ({size_mb:.1f} MB)" if size_mb > 0 else ""

    print("\n" + "=" * 65)
    print(f"  [NEW UPDATE AVAILABLE]  v{curr_v}  -->  v{latest_v}{size_label}")
    print("=" * 65)
    print("  Release Notes:")
    print(_format_release_notes_preview(update_info.get("release_notes", "")))
    print("-" * 65)
    print("  (Note: Your config.json, complaints_state.db, complaints.xlsx,")
    print("   Today_Complaints/, and logs/ are 100% preserved during updates.)")
    print("=" * 65)

    try:
        ans = input(f"\nInstall update v{latest_v} and restart now? [Y/n]: ").strip().lower()
    except (EOFError, StopIteration, KeyboardInterrupt):
        print("\n  [i] Update skipped.")
        return False

    if ans in ["", "y", "yes"]:
        return perform_self_update(update_info, config=config)

    print("  [i] Update postponed. You can select [U] from the Main Menu at any time.")
    return False


def run_startup_update_check(config=None):
    """
    Performs a fast (default 3s) non-blocking startup check for new releases.
    Silently ignores offline/timeout states so daily ERP operations are never delayed.
    """
    cleanup_update_artifacts()
    updater_cfg = config_manager.get_updater_settings(config)
    if not updater_cfg.get("check_on_startup", True):
        return

    timeout = updater_cfg.get("startup_timeout_seconds", 3.0)
    info = check_for_updates(config=config, timeout=timeout)
    if info.get("error") or not info.get("checked"):
        return

    if info.get("update_available"):
        prompt_and_install_update(info, config=config)


def run_interactive_update_check(config=None):
    """
    Triggered manually via [U] Check for Updates in the Main Menu.
    Uses a 10-second timeout and prints explicit status feedback.
    """
    cleanup_update_artifacts()
    updater_cfg = config_manager.get_updater_settings(config)
    repo = updater_cfg.get("github_repo", version.GITHUB_REPO)

    print("\n" + "-" * 60)
    print("             SOFTWARE UPDATE CHECK")
    print("-" * 60)
    print(f"  Current Installed Version : v{version.__version__}")
    print(f"  Release Channel (GitHub)  : {repo}")
    print("  Checking GitHub Releases for updates...")

    info = check_for_updates(config=config, timeout=10.0)
    if info.get("error"):
        logger.warning("\n  [!] Could not check for updates: %s", info["error"])
        print(f"      Manual download URL: https://github.com/{repo}/releases")
        return

    if not info.get("update_available"):
        print(
            f"\n  [✓] You are running the latest version (v{version.__version__}). No update needed!"
        )
        return

    prompt_and_install_update(info, config=config)
