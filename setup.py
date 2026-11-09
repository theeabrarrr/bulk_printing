"""
setup.py - Backward-compatibility alias for calibration_wizard.py.
Prevents namespace collisions with standard Python setuptools while preserving CLI entry points.
"""
from calibration_wizard import *

if __name__ == "__main__":
    run_calibration_wizard()
