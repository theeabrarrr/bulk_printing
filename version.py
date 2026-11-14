import re

__version__ = "6.1.0"
APP_NAME = "ERP_Automation_Suite"
EXE_NAME = "ERP_Automation_Suite"
GITHUB_REPO = "theeabrarrr/bulk_printing"


def normalize_version_string(version_str: str) -> str:
    """Strips whitespace and leading 'v'/'V' prefix from a version string."""
    if not version_str:
        return "0.0.0"
    cleaned = str(version_str).strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:].strip()
    return cleaned or "0.0.0"


def parse_version(version_str: str):
    """
    Parses a semantic version string (e.g., 'v6.1.0', '6.0', '6.1.0-beta.1') into
    a comparable tuple: (major, minor, patch, is_final_release, prerelease_tokens).
    In SemVer, 6.1.0 (is_final_release=1) > 6.1.0-beta.1 (is_final_release=0).
    Raises ValueError if the core version does not start with numeric components.
    """
    cleaned = normalize_version_string(version_str)
    # Strip build metadata (+...)
    cleaned = cleaned.split("+", 1)[0]

    if "-" in cleaned:
        core_part, prerelease_part = cleaned.split("-", 1)
    else:
        core_part, prerelease_part = cleaned, ""

    raw_nums = core_part.split(".")
    if not raw_nums or not all(part.isdigit() for part in raw_nums):
        raise ValueError(f"Invalid semantic version string: '{version_str}'")

    nums = [int(p) for p in raw_nums]
    while len(nums) < 3:
        nums.append(0)

    major, minor, patch = nums[0], nums[1], nums[2]
    if prerelease_part:
        pre_tokens = []
        for tok in prerelease_part.split("."):
            pre_tokens.append((0, int(tok)) if tok.isdigit() else (1, tok.lower()))
        return (major, minor, patch, 0, tuple(pre_tokens))
    return (major, minor, patch, 1, ())


def is_newer_version(remote_version: str, current_version: str = __version__) -> bool:
    """
    Returns True if remote_version is strictly newer than current_version.
    Returns False if versions are equal, older, or unparseable.
    """
    try:
        remote_parsed = parse_version(remote_version)
        current_parsed = parse_version(current_version)
        return remote_parsed > current_parsed
    except Exception:
        return False


def get_release_zip_name(ver: str = __version__) -> str:
    """Returns canonical release ZIP filename, e.g. 'ERP_Automation_Suite_v6.1.0.zip'."""
    norm = normalize_version_string(ver)
    return f"{APP_NAME}_v{norm}.zip"


def get_release_sha256_name(ver: str = __version__) -> str:
    """Returns canonical release SHA-256 sidecar filename, e.g. 'ERP_Automation_Suite_v6.1.0.zip.sha256'."""
    return f"{get_release_zip_name(ver)}.sha256"


def extract_sha256_hex(text: str) -> str:
    """Extracts the first 64-character hexadecimal SHA-256 hash from a string or .sha256 file."""
    if not text:
        return ""
    # Check for 'sha256:<64hex>' format (e.g. GitHub API asset.digest)
    m_prefixed = re.search(r"sha256:([a-fA-F0-9]{64})\b", str(text))
    if m_prefixed:
        return m_prefixed.group(1).lower()
    m_raw = re.search(r"\b([a-fA-F0-9]{64})\b", str(text))
    if m_raw:
        return m_raw.group(1).lower()
    return ""
