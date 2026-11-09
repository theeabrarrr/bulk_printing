import os
import sys
import shutil
import subprocess

try:
    import config_manager
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
        expected_base = os.path.dirname(fake_exe_dir)

        print(f"  Simulated Executable: {fake_exe_dir}")
        print(f"  Resolved Base Dir:    {resolved_base}")
        print(f"  Resolved Config Path: {resolved_cfg}")

        assert resolved_base == expected_base, f"Mismatch: {resolved_base} != {expected_base}"
        print("  [+] One-Dir frozen path anchoring verified successfully!")
    finally:
        if orig_frozen:
            sys.frozen = orig_frozen
        else:
            if hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        sys.executable = orig_executable


def build_executable():
    """Runs PyInstaller to package main.py into a standalone --onedir suite."""
    print("=" * 60)
    print(f"  BUILDING ONEDIR SUITE: {PACKAGE_NAME} ({EXE_NAME}.exe)")
    print("=" * 60)

    verify_frozen_path_anchoring()

    if not check_pyinstaller():
        print("\n[!] PyInstaller is not installed in the current environment.")
        print("    Install it with: pip install pyinstaller")
        return False

    dist_root = os.path.join(PROJECT_DIR, "dist")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--console",
        "--clean",
        "--distpath", dist_root,
        "--workpath", os.path.join(PROJECT_DIR, "build"),
        "--specpath", PROJECT_DIR,
        "--name",
        PACKAGE_NAME,
        "--collect-all", "pypdfium2",
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

    if result.returncode != 0:
        print(f"\n[!] Build failed with exit code {result.returncode}.")
        return False

    suite_dir = os.path.join(dist_root, PACKAGE_NAME)
    exe_path = os.path.join(suite_dir, f"{EXE_NAME}.exe")

    if os.path.exists(exe_path):
        # Copy default config.json only if not already present in dist/ERP_Automation_Suite/ (preserves user calibration)
        src_cfg = os.path.join(PROJECT_DIR, "config.json")
        dest_cfg = os.path.join(suite_dir, "config.json")
        if os.path.exists(src_cfg):
            if not os.path.exists(dest_cfg):
                shutil.copy2(src_cfg, dest_cfg)
                print(f"  [+] Config initialized: {dest_cfg}")
            else:
                print(f"  [*] Preserved existing calibrated config: {dest_cfg}")

        # Copy sample/active complaints.xlsx if not present in suite folder
        src_xlsx = os.path.join(PROJECT_DIR, "complaints.xlsx")
        dest_xlsx = os.path.join(suite_dir, "complaints.xlsx")
        if os.path.exists(src_xlsx) and not os.path.exists(dest_xlsx):
            shutil.copy2(src_xlsx, dest_xlsx)
            print(f"  [+] Input dataset deployed to: {dest_xlsx}")

        # Ensure completed_complaints.txt exists
        dest_tracker = os.path.join(suite_dir, "completed_complaints.txt")
        if not os.path.exists(dest_tracker):
            with open(dest_tracker, "w", encoding="utf-8") as f:
                pass
            print(f"  [+] Initialized tracking file: {dest_tracker}")

        # Ensure printed_complaints.txt exists
        dest_print_tracker = os.path.join(suite_dir, "printed_complaints.txt")
        if not os.path.exists(dest_print_tracker):
            with open(dest_print_tracker, "w", encoding="utf-8") as f:
                pass
            print(f"  [+] Initialized print tracking file: {dest_print_tracker}")

        print("\n" + "=" * 60)
        print("  BUILD SUCCESSFUL!")
        print(f"  Distribution Suite: {suite_dir}")
        print(f"  Executable Location: {exe_path}")
        print("=" * 60)
        return True

    print(f"\n[!] Build completed but executable was not found at {exe_path}.")
    return False


if __name__ == "__main__":
    success = build_executable()
    sys.exit(0 if success else 1)
