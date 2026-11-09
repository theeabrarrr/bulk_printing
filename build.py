import os
import sys
import shutil
import subprocess
import config_manager


EXE_NAME = "ERP_Bulk_Printer"
ENTRY_SCRIPT = "main.py"


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
    to verify config_manager resolves paths next to the executable,
    NOT inside a temporary _MEIxxxx extraction folder.
    """
    print("\n--- Verifying Portable Path Anchoring (Simulated Frozen State) ---")
    fake_exe_dir = os.path.abspath(os.path.join("dist", f"{EXE_NAME}.exe"))
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
        print("  [+] Frozen path anchoring verified successfully!")
    finally:
        if orig_frozen:
            sys.frozen = orig_frozen
        else:
            if hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        sys.executable = orig_executable


def build_executable():
    """Runs PyInstaller to package main.py into a standalone .exe."""
    print("=" * 60)
    print(f"  BUILDING STANDALONE EXECUTABLE: {EXE_NAME}.exe")
    print("=" * 60)

    verify_frozen_path_anchoring()

    if not check_pyinstaller():
        print("\n[!] PyInstaller is not installed in the current environment.")
        print("    Install it with: pip install pyinstaller")
        return False

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--console",
        "--clean",
        "--name",
        EXE_NAME,
        # Exclude heavy unused libraries present in global Python to keep .exe compact and fast
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
        ENTRY_SCRIPT,
    ]

    print(f"\nRunning command:\n  {' '.join(cmd)}\n")
    result = subprocess.run(cmd)

    if result.returncode != 0:
        print(f"\n[!] Build failed with exit code {result.returncode}.")
        return False

    dist_dir = os.path.abspath("dist")
    exe_path = os.path.join(dist_dir, f"{EXE_NAME}.exe")

    if os.path.exists(exe_path):
        # Copy default config.json alongside the compiled .exe in dist/ for immediate portability
        src_cfg = os.path.abspath("config.json")
        if os.path.exists(src_cfg):
            shutil.copy2(src_cfg, os.path.join(dist_dir, "config.json"))
        print("\n" + "=" * 60)
        print("  BUILD SUCCESSFUL!")
        print(f"  Executable Location: {exe_path}")
        print(f"  Config Copied To:    {os.path.join(dist_dir, 'config.json')}")
        print("=" * 60)
        return True

    print("\n[!] Build completed but executable was not found in dist/.")
    return False


if __name__ == "__main__":
    success = build_executable()
    sys.exit(0 if success else 1)
