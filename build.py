import os
import sys
import shutil
import subprocess
import tempfile

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
PACKAGE_NAME = "ERP_Automation_Suite"
EXE_NAME = "ERP_Automation_Suite"
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
    """Terminates any running ERP_Automation_Suite.exe processes and evacuates locked DLLs so PyInstaller can rebuild cleanly."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", f"{EXE_NAME}.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    if suite_dir and os.path.exists(suite_dir):
        for target in (os.path.join(suite_dir, f"{EXE_NAME}.exe"), os.path.join(suite_dir, "_internal")):
            if not os.path.exists(target):
                continue
            if os.path.isfile(target):
                try:
                    os.remove(target)
                except PermissionError:
                    try:
                        shutil.move(target, tempfile.mktemp(prefix="old_exe_"))
                    except Exception:
                        pass
            else:
                for root, dirs, files in os.walk(target, topdown=False):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            os.remove(fp)
                        except PermissionError:
                            try:
                                shutil.move(fp, tempfile.mktemp(prefix="old_dll_"))
                            except Exception:
                                pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except OSError:
                            pass
                try:
                    os.rmdir(target)
                except OSError:
                    pass


def build_executable():
    """Runs PyInstaller to package main.py into a standalone --onedir suite."""
    print("=" * 60)
    print(f"  BUILDING ONEDIR SUITE: {PACKAGE_NAME} ({EXE_NAME}.exe)")
    print("=" * 60)

    dist_root = os.path.join(PROJECT_DIR, "dist")
    suite_dir = os.path.join(dist_root, PACKAGE_NAME)
    _terminate_running_suite_processes(suite_dir)
    verify_frozen_path_anchoring()

    if not check_pyinstaller():
        print("\n[!] PyInstaller is not installed in the current environment.")
        print("    Install it with: pip install pyinstaller")
        return False

    dist_root = os.path.join(PROJECT_DIR, "dist")
    suite_dir = os.path.join(dist_root, PACKAGE_NAME)

    with tempfile.TemporaryDirectory() as temp_root:
        backup_dir = os.path.join(temp_root, "backup")
        stage_dist = os.path.join(temp_root, "stage_dist")
        os.makedirs(backup_dir, exist_ok=True)
        os.makedirs(stage_dist, exist_ok=True)

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
            "--workpath", os.path.join(PROJECT_DIR, "build"),
            "--specpath", PROJECT_DIR,
            "--name",
            PACKAGE_NAME,
            "--collect-all", "pypdfium2",
            "--hidden-import", "sqlite3",
            "--hidden-import", "win32print",
            "--hidden-import", "win32ui",
            "--hidden-import", "win32con",
            "--hidden-import", "win32gui",
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

        if result.returncode == 0:
            staged_suite = os.path.join(stage_dist, PACKAGE_NAME)
            os.makedirs(suite_dir, exist_ok=True)
            _terminate_running_suite_processes(suite_dir)
            for item in os.listdir(staged_suite):
                s_item = os.path.join(staged_suite, item)
                d_item = os.path.join(suite_dir, item)
                if os.path.isdir(s_item):
                    shutil.copytree(s_item, d_item, dirs_exist_ok=True)
                else:
                    shutil.copy2(s_item, d_item)

        if preserved_items:
            _restore_runtime_data(backup_dir, suite_dir, preserved_items)

    if result.returncode != 0:
        print(f"\n[!] Build failed with exit code {result.returncode}.")
        return False

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

        print("\n" + "=" * 60)
        print("  BUILD SUCCESSFUL!")
        print(f"  Distribution Suite: {suite_dir}")
        print(f"  Executable Location: {exe_path}")
        print("=" * 60)

        # Clean up temporary PyInstaller workpath (build/), .spec file, and any root logs/ created during build
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
            for d in (build_work_dir, root_logs_dir, root_pycache_dir):
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(spec_file):
                os.remove(spec_file)
            print("  [+] Cleaned up temporary PyInstaller build/, .spec, and root cache artifacts.")
        except Exception as cleanup_err:
            print(f"  [!] Notice: Could not remove temporary build artifacts: {cleanup_err}")

        return True

    print(f"\n[!] Build completed but executable was not found at {exe_path}.")
    return False


if __name__ == "__main__":
    success = build_executable()
    sys.exit(0 if success else 1)
