import argparse
import hashlib
import os
import sys
import shutil
import subprocess
import tempfile
import zipfile

import version

try:
    import config_manager
    import state_manager
except ImportError as e:
    if getattr(e, "name", None) == "pyautogui":
        print("\n[!] Missing required dependency: 'pyautogui'.")
        print("    Please install dependencies with: pip install -r requirements.txt")
        sys.exit(1)
    raise


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_NAME = version.APP_NAME
EXE_NAME = version.EXE_NAME
ENTRY_SCRIPT = os.path.join(PROJECT_DIR, "main.py")
RUNTIME_PRESERVE_ITEMS = [
    "config.json",
    "complaints.xlsx",
    "complaints_state.db",
    "complaints_state.db-wal",
    "complaints_state.db-shm",
    "completed_complaints.txt",
    "printed_complaints.txt",
    "Today_Complaints",
    "logs",
]


def check_pyinstaller():
    """Checks if PyInstaller is installed in the active Python environment."""
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def verify_frozen_path_anchoring():
    """
    Simulates frozen PyInstaller runtime state (sys.frozen = True)
    under the new --onedir directory structure:
    dist/ERP_Automation_Suite/ERP_Automation_Suite.exe
    Verifies that config_manager resolves paths next to the executable,
    permanently bypassing Windows %TEMP% extraction and permission locks.
    """
    print("\n--- Verifying Portable Path Anchoring (One-Dir Frozen State) ---")
    fake_exe_dir = os.path.join(PROJECT_DIR, "dist", PACKAGE_NAME, f"{EXE_NAME}.exe")
    orig_frozen = getattr(sys, "frozen", False)
    orig_executable = sys.executable

    try:
        sys.frozen = True
        sys.executable = fake_exe_dir
        resolved_base = config_manager.get_base_dir()
        resolved_cfg = config_manager.get_config_path()
        resolved_db = config_manager.get_database_path()
        expected_base = os.path.dirname(fake_exe_dir)

        print(f"  Simulated Executable: {fake_exe_dir}")
        print(f"  Resolved Base Dir:    {resolved_base}")
        print(f"  Resolved Config Path: {resolved_cfg}")
        print(f"  Resolved DB Path:     {resolved_db}")

        assert resolved_base == expected_base, f"Mismatch: {resolved_base} != {expected_base}"
        assert os.path.dirname(resolved_db) == expected_base, f"DB Path mismatch: {resolved_db}"
        print("  [+] One-Dir frozen path anchoring verified successfully!")
    finally:
        if orig_frozen:
            sys.frozen = orig_frozen
        else:
            if hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        sys.executable = orig_executable


def _backup_runtime_data(suite_dir, backup_dir):
    """Preserves existing runtime files, SQLite DB, logs/, and Today_Complaints/ before PyInstaller wipes suite_dir."""
    preserved = []
    if not os.path.exists(suite_dir):
        return preserved

    items_to_check = list(RUNTIME_PRESERVE_ITEMS)
    try:
        for entry in os.listdir(suite_dir):
            if (
                (entry.startswith("printed_complaints_") and entry.endswith(".bak"))
                or (entry.startswith("complaints_state_") and entry.endswith(".db.bak"))
                or entry.endswith(".migrated.bak")
            ):
                if entry not in items_to_check:
                    items_to_check.append(entry)
    except Exception:
        pass

    for item in items_to_check:
        src = os.path.join(suite_dir, item)
        if os.path.exists(src):
            dst = os.path.join(backup_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            preserved.append(item)
    return preserved


def _restore_runtime_data(backup_dir, suite_dir, preserved_items):
    """Restores preserved runtime files, SQLite DB, logs/, and Today_Complaints/ into suite_dir after build."""
    os.makedirs(suite_dir, exist_ok=True)
    for item in preserved_items:
        src = os.path.join(backup_dir, item)
        dst = os.path.join(suite_dir, item)
        if os.path.exists(src):
            if os.path.isdir(src):
                for root, _, files in os.walk(src):
                    rel_dir = os.path.relpath(root, src)
                    target_dir = dst if rel_dir == "." else os.path.join(dst, rel_dir)
                    os.makedirs(target_dir, exist_ok=True)
                    for fname in files:
                        s_file = os.path.join(root, fname)
                        d_file = os.path.join(target_dir, fname)
                        if not os.path.exists(d_file):
                            try:
                                shutil.copy2(s_file, d_file)
                            except PermissionError:
                                pass
            else:
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
                    except PermissionError:
                        pass
            print(f"  [*] Preserved existing runtime data: {dst}")


def _terminate_running_suite_processes(suite_dir=None):
    """Terminates any running ERP_Automation_Suite.exe processes without deleting installed application files."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", f"{EXE_NAME}.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            pass


def _remove_suite_target(target):
    """Removes an application file or directory tree from suite_dir, evacuating locked files if necessary."""
    if not target or not os.path.exists(target):
        return
    if os.path.isfile(target) or os.path.islink(target):
        try:
            os.remove(target)
        except PermissionError:
            try:
                shutil.move(target, tempfile.mktemp(prefix="old_exe_"))
            except Exception as e:
                raise OSError(f"Could not remove locked file '{target}': {e}") from e
    else:
        for root, dirs, files in os.walk(target, topdown=False):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    os.remove(fp)
                except PermissionError:
                    try:
                        shutil.move(fp, tempfile.mktemp(prefix="old_dll_"))
                    except Exception as e:
                        raise OSError(f"Could not remove locked file '{fp}': {e}") from e
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            os.rmdir(target)
        except OSError:
            shutil.rmtree(target, ignore_errors=True)


def _deploy_staged_suite(staged_suite, suite_dir, rollback_dir):
    """
    Deploys staged PyInstaller output into suite_dir while keeping a rollback copy
    of the previous installation and restoring it automatically if deployment fails.
    """
    staged_exe = os.path.join(staged_suite, f"{EXE_NAME}.exe")
    if not os.path.isdir(staged_suite) or not os.path.isfile(staged_exe):
        raise FileNotFoundError(f"Staged executable not found at '{staged_exe}'.")

    _terminate_running_suite_processes()
    os.makedirs(suite_dir, exist_ok=True)
    os.makedirs(rollback_dir, exist_ok=True)

    staged_items = os.listdir(staged_suite)
    app_targets = []
    for name in [f"{EXE_NAME}.exe", "_internal"] + list(staged_items):
        if name not in app_targets and name not in RUNTIME_PRESERVE_ITEMS:
            app_targets.append(name)

    rolled_back_items = []
    for name in app_targets:
        existing_path = os.path.join(suite_dir, name)
        if os.path.exists(existing_path):
            rb_path = os.path.join(rollback_dir, name)
            if os.path.isdir(existing_path):
                shutil.copytree(existing_path, rb_path, dirs_exist_ok=True)
            else:
                shutil.copy2(existing_path, rb_path)
            rolled_back_items.append(name)

    try:
        for name in rolled_back_items:
            _remove_suite_target(os.path.join(suite_dir, name))

        for item in staged_items:
            s_item = os.path.join(staged_suite, item)
            d_item = os.path.join(suite_dir, item)
            if os.path.isdir(s_item):
                shutil.copytree(s_item, d_item, dirs_exist_ok=True)
            else:
                shutil.copy2(s_item, d_item)

        deployed_exe = os.path.join(suite_dir, f"{EXE_NAME}.exe")
        if not os.path.isfile(deployed_exe):
            raise RuntimeError(f"Deployment verification failed: '{deployed_exe}' is missing.")
    except Exception as deploy_err:
        print(f"  [!] Deployment failed ({deploy_err}); restoring previous installation from rollback copy...")
        for name in app_targets:
            try:
                _remove_suite_target(os.path.join(suite_dir, name))
            except Exception:
                pass
        for name in rolled_back_items:
            rb_src = os.path.join(rollback_dir, name)
            rb_dst = os.path.join(suite_dir, name)
            if os.path.exists(rb_src):
                try:
                    if os.path.isdir(rb_src):
                        shutil.copytree(rb_src, rb_dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(rb_src, rb_dst)
                except Exception as rb_err:
                    print(f"  [!] Rollback warning for '{name}': {rb_err}")
        raise


def _cleanup_build_artifacts(root_logs_existed, preexisting_log_entries):
    """Cleans up temporary PyInstaller workpath, .spec, __pycache__, and build-only root logs."""
    try:
        import logger_manager
        logger_manager.close_logging_handlers()
    except Exception:
        pass

    build_work_dir = os.path.join(PROJECT_DIR, "build")
    spec_file = os.path.join(PROJECT_DIR, f"{PACKAGE_NAME}.spec")
    root_logs_dir = os.path.join(PROJECT_DIR, "logs")
    root_pycache_dir = os.path.join(PROJECT_DIR, "__pycache__")
    try:
        for d in (build_work_dir, root_pycache_dir):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)

        if os.path.exists(root_logs_dir):
            if not root_logs_existed:
                shutil.rmtree(root_logs_dir, ignore_errors=True)
            else:
                for entry in list(os.listdir(root_logs_dir)):
                    if entry not in preexisting_log_entries:
                        entry_path = os.path.join(root_logs_dir, entry)
                        if os.path.isdir(entry_path):
                            shutil.rmtree(entry_path, ignore_errors=True)
                        else:
                            try:
                                os.remove(entry_path)
                            except OSError:
                                pass

        if os.path.exists(spec_file):
            os.remove(spec_file)
        print("  [+] Cleaned up temporary PyInstaller build/, .spec, and root cache artifacts.")
    except Exception as cleanup_err:
        print(f"  [!] Notice: Could not remove temporary build artifacts: {cleanup_err}")


def create_release_archive(suite_dir, dist_root, ver=None):
    """
    Packages ONLY the application binaries (ERP_Automation_Suite.exe and _internal/)
    into dist/ERP_Automation_Suite_vX.Y.Z.zip and writes its SHA-256 sidecar file
    dist/ERP_Automation_Suite_vX.Y.Z.zip.sha256.
    Strictly excludes runtime data (config.json, complaints.xlsx, complaints_state.db,
    Today_Complaints/, logs/, etc.).
    """
    if ver is None:
        ver = version.__version__
    norm_ver = version.normalize_version_string(ver)
    zip_filename = version.get_release_zip_name(norm_ver)
    sha256_filename = version.get_release_sha256_name(norm_ver)
    zip_path = os.path.join(dist_root, zip_filename)
    sha256_path = os.path.join(dist_root, sha256_filename)

    exe_file = os.path.join(suite_dir, f"{EXE_NAME}.exe")
    internal_dir = os.path.join(suite_dir, "_internal")
    if not os.path.isfile(exe_file) or not os.path.isdir(internal_dir):
        raise FileNotFoundError(
            f"Cannot create release archive: missing '{exe_file}' or '{internal_dir}'."
        )

    os.makedirs(dist_root, exist_ok=True)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    print(f"\n[*] Creating clean release archive: {zip_path} ...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. Add ERP_Automation_Suite.exe under top-level ERP_Automation_Suite/ folder
        zf.write(exe_file, os.path.join(PACKAGE_NAME, f"{EXE_NAME}.exe"))

        # 2. Add _internal/ directory tree recursively
        for root, _, files in os.walk(internal_dir):
            for fname in sorted(files):
                full_p = os.path.join(root, fname)
                rel_p = os.path.relpath(full_p, suite_dir)
                arcname = os.path.join(PACKAGE_NAME, rel_p)
                zf.write(full_p, arcname)

    # Compute SHA-256 checksum
    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    sha256_hex = hasher.hexdigest().lower()

    with open(sha256_path, "w", encoding="utf-8", newline="\n") as sf:
        sf.write(f"{sha256_hex}  {zip_filename}\n")

    zip_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  [+] Release ZIP created : {zip_path} ({zip_mb:.2f} MB)")
    print(f"  [+] SHA-256 Checksum    : {sha256_hex}")
    print(f"  [+] SHA-256 Sidecar     : {sha256_path}")
    return zip_path, sha256_path, sha256_hex


def publish_github_release(zip_path, sha256_path, sha256_hex, ver=None, notes=None):
    """
    Publishes or updates the GitHub Release vX.Y.Z using the GitHub CLI (gh).
    """
    if ver is None:
        ver = version.__version__
    norm_ver = version.normalize_version_string(ver)
    tag = f"v{norm_ver}"
    repo = version.GITHUB_REPO

    if not shutil.which("gh"):
        print("\n[!] GitHub CLI ('gh') is not installed or not on PATH.")
        print("    Install it from https://cli.github.com/ or upload the release manually:")
        print(f"    1. Asset: {zip_path}")
        print(f"    2. Asset: {sha256_path}")
        return False

    release_title = f"{PACKAGE_NAME} {tag}"
    default_body = (
        f"Automated release of **{PACKAGE_NAME} {tag}**.\n\n"
        f"### Integrity Checksum (SHA-256)\n"
        f"```\n{sha256_hex}  {os.path.basename(zip_path)}\n```"
    )
    body_text = f"{notes.strip()}\n\n{default_body}" if notes and notes.strip() else default_body

    print(f"\n[*] Publishing GitHub Release {tag} to {repo}...")
    view_res = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if view_res.returncode == 0:
        print(f"  [*] Release {tag} already exists; uploading updated assets (--clobber)...")
        cmd = ["gh", "release", "upload", tag, zip_path, sha256_path, "--clobber", "--repo", repo]
    else:
        cmd = [
            "gh",
            "release",
            "create",
            tag,
            zip_path,
            sha256_path,
            "--repo",
            repo,
            "--title",
            release_title,
            "--notes",
            body_text,
        ]

    res = subprocess.run(cmd, cwd=PROJECT_DIR, check=False)
    if res.returncode == 0:
        print(f"  [+] Successfully published release {tag} to https://github.com/{repo}/releases/tag/{tag}")
        return True
    print(f"  [!] 'gh release' exited with code {res.returncode}.")
    return False


def build_executable(publish=False, release_notes=None):
    """Runs PyInstaller to package main.py into a standalone --onedir suite and builds the release ZIP."""
    print("=" * 60)
    print(f"  BUILDING ONEDIR SUITE: {PACKAGE_NAME} v{version.__version__} ({EXE_NAME}.exe)")
    print("=" * 60)

    root_logs_dir = os.path.join(PROJECT_DIR, "logs")
    root_logs_existed = os.path.exists(root_logs_dir)
    preexisting_log_entries = (
        set(os.listdir(root_logs_dir))
        if root_logs_existed and os.path.isdir(root_logs_dir)
        else set()
    )

    dist_root = os.path.join(PROJECT_DIR, "dist")
    suite_dir = os.path.join(dist_root, PACKAGE_NAME)
    _terminate_running_suite_processes()
    verify_frozen_path_anchoring()

    if not check_pyinstaller():
        print("\n[!] PyInstaller is not installed in the current environment.")
        print("    Install it with: pip install pyinstaller")
        return False

    with tempfile.TemporaryDirectory() as temp_root:
        backup_dir = os.path.join(temp_root, "backup")
        stage_dist = os.path.join(temp_root, "stage_dist")
        work_dir = os.path.join(temp_root, "build_work")
        spec_dir = os.path.join(temp_root, "spec")
        rollback_dir = os.path.join(temp_root, "rollback")
        build_logs_dir = os.path.join(temp_root, "build_logs")
        os.makedirs(backup_dir, exist_ok=True)
        os.makedirs(stage_dist, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(spec_dir, exist_ok=True)
        os.makedirs(rollback_dir, exist_ok=True)
        os.makedirs(build_logs_dir, exist_ok=True)

        try:
            import logger_manager
            logger_manager.setup_logging(
                {"logging": {"log_dir": build_logs_dir, "log_file": "build.log"}},
                force_reinit=True,
            )
        except Exception:
            pass

        preserved_items = _backup_runtime_data(suite_dir, backup_dir)
        if preserved_items:
            print(f"\n[*] Backed up {len(preserved_items)} runtime item(s) from '{suite_dir}': {', '.join(preserved_items)}")

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--console",
            "--clean",
            "--distpath", stage_dist,
            "--workpath", work_dir,
            "--specpath", spec_dir,
            "--name",
            PACKAGE_NAME,
            "--collect-all", "pypdfium2",
            "--hidden-import", "sqlite3",
            "--hidden-import", "win32print",
            "--hidden-import", "win32ui",
            "--hidden-import", "win32con",
            "--hidden-import", "win32gui",
            "--hidden-import=win32timezone",
            "--hidden-import", "PIL.ImageWin",
            # Exclude heavy unused libraries present in global Python to keep binary compact and fast
            "--exclude-module", "matplotlib",
            "--exclude-module", "scipy",
            "--exclude-module", "sklearn",
            "--exclude-module", "scikit-image",
            "--exclude-module", "cv2",
            "--exclude-module", "onnxruntime",
            "--exclude-module", "streamlit",
            "--exclude-module", "fastapi",
            "--exclude-module", "flask",
            "--exclude-module", "sqlalchemy",
            "--exclude-module", "grpc",
            "--exclude-module", "google",
            "--exclude-module", "pyarrow",
            "--exclude-module", "numba",
            "--exclude-module", "llvmlite",
            "--exclude-module", "jinja2",
            "--exclude-module", "IPython",
            "--exclude-module", "tornado",
            ENTRY_SCRIPT,
        ]

        print(f"\nRunning command:\n  {' '.join(cmd)}\n")
        result = subprocess.run(cmd, cwd=PROJECT_DIR)

        if result.returncode != 0:
            try:
                import logger_manager
                logger_manager.close_logging_handlers()
            except Exception:
                pass
            print(f"\n[!] Build failed with exit code {result.returncode}.")
            return False

        staged_suite = os.path.join(stage_dist, PACKAGE_NAME)
        try:
            _deploy_staged_suite(staged_suite, suite_dir, rollback_dir)
        except Exception as deploy_err:
            if preserved_items:
                _restore_runtime_data(backup_dir, suite_dir, preserved_items)
            try:
                import logger_manager
                logger_manager.close_logging_handlers()
            except Exception:
                pass
            print(f"\n[!] Deployment of staged build failed: {deploy_err}")
            return False

        if preserved_items:
            _restore_runtime_data(backup_dir, suite_dir, preserved_items)

        exe_path = os.path.join(suite_dir, f"{EXE_NAME}.exe")
        if os.path.exists(exe_path):
            # Copy default config.json only if not already present in dist/ERP_Automation_Suite/ (preserves user calibration)
            src_cfg = os.path.join(PROJECT_DIR, "config.json")
            dest_cfg = os.path.join(suite_dir, "config.json")
            if os.path.exists(src_cfg) and not os.path.exists(dest_cfg):
                shutil.copy2(src_cfg, dest_cfg)
                print(f"  [+] Config initialized: {dest_cfg}")

            # Copy sample/active complaints.xlsx if not present in suite folder
            src_xlsx = os.path.join(PROJECT_DIR, "complaints.xlsx")
            dest_xlsx = os.path.join(suite_dir, "complaints.xlsx")
            if os.path.exists(src_xlsx) and not os.path.exists(dest_xlsx):
                shutil.copy2(src_xlsx, dest_xlsx)
                print(f"  [+] Input dataset deployed to: {dest_xlsx}")

            # Ensure logs/ directory exists in suite folder
            dest_logs = os.path.join(suite_dir, "logs")
            os.makedirs(dest_logs, exist_ok=True)
            print(f"  [+] Initialized rotating logs directory: {dest_logs}")

            # Initialize SQLite state database, migrate any legacy .txt files, and sync Today_Complaints/ in suite_dir
            dest_db = os.path.join(suite_dir, "complaints_state.db")
            dest_completed_txt = os.path.join(suite_dir, "completed_complaints.txt")
            dest_printed_txt = os.path.join(suite_dir, "printed_complaints.txt")
            dest_out_dir = os.path.join(suite_dir, "Today_Complaints")
            state_manager.init_db(dest_db, output_dir=dest_out_dir, sync_disk=False)
            if os.path.exists(dest_completed_txt) or os.path.exists(dest_printed_txt):
                state_manager.migrate_legacy_txt_files(
                    db_path=dest_db,
                    completed_txt_path=dest_completed_txt,
                    printed_txt_path=dest_printed_txt,
                    output_dir=dest_out_dir,
                    archive_after_migrate=True,
                )
            state_manager.sync_disk_to_db(dest_db, output_dir=dest_out_dir, announce=True)
            print(f"  [+] Initialized SQLite state database: {dest_db}")

        try:
            import logger_manager
            logger_manager.close_logging_handlers()
        except Exception:
            pass

    exe_path = os.path.join(suite_dir, f"{EXE_NAME}.exe")
    if os.path.exists(exe_path):
        zip_path, sha256_path, sha256_hex = create_release_archive(
            suite_dir=suite_dir,
            dist_root=dist_root,
            ver=version.__version__,
        )

        print("\n" + "=" * 60)
        print(f"  BUILD SUCCESSFUL! (v{version.__version__})")
        print(f"  Distribution Suite : {suite_dir}")
        print(f"  Executable Location: {exe_path}")
        print(f"  Release ZIP Asset  : {zip_path}")
        print(f"  Release SHA-256    : {sha256_hex}")
        print("=" * 60)

        _cleanup_build_artifacts(root_logs_existed, preexisting_log_entries)

        if publish:
            publish_github_release(
                zip_path=zip_path,
                sha256_path=sha256_path,
                sha256_hex=sha256_hex,
                ver=version.__version__,
                notes=release_notes,
            )
        else:
            print(
                f"\n  [i] Tip: Run `python -B build.py --publish` to publish Release v{version.__version__} directly to GitHub."
            )
        return True

    print(f"\n[!] Build completed but executable was not found at {exe_path}.")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Build {PACKAGE_NAME} (--onedir) and generate GitHub Release assets."
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Automatically create/upload the GitHub Release via 'gh release'.",
    )
    parser.add_argument(
        "--notes",
        type=str,
        default=None,
        help="Optional release notes to include when using --publish.",
    )
    args = parser.parse_args()
    success = build_executable(publish=args.publish, release_notes=args.notes)
    sys.exit(0 if success else 1)
