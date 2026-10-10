import re
import subprocess
import sys
from collections.abc import Callable

from .translate import NO_WINDOW, find_cli

INSTALL_URL_PS1 = "https://antigravity.google/cli/install.ps1"
INSTALL_URL_SH = "https://antigravity.google/cli/install.sh"


def install_command() -> list[str]:
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                f"irm {INSTALL_URL_PS1} | iex"]
    return ["bash", "-c", f"curl -fsSL {INSTALL_URL_SH} | bash"]


def install_agy(log: Callable[[str], None]) -> str:
    """Run the official installer and return the path of the installed binary."""
    proc = subprocess.Popen(
        install_command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    assert proc.stdout
    for line in proc.stdout:
        log(line.rstrip())
    if proc.wait() != 0:
        raise RuntimeError(f"agy installer exited with {proc.returncode}")
    path = find_cli("agy")
    if not path:
        raise RuntimeError("agy installer finished but the binary was not found")
    return path


def login_command(exe: str) -> list[str]:
    """Open an interactive agy session in a new terminal so the user can sign in."""
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "Antigravity sign-in", exe]
    if sys.platform == "darwin":
        return ["open", "-a", "Terminal", exe]
    return ["x-terminal-emulator", "-e", exe]


def list_models() -> list[tuple[str, str]]:
    """(id, display name) pairs from `agy models`; empty when agy is missing or fails."""
    exe = find_cli("agy")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    models = []
    for line in proc.stdout.splitlines():
        model_id, _, label = line.partition("\t")
        model_id = model_id.strip()
        if model_id and " " not in model_id:
            models.append((model_id, label.strip() or model_id))
    return models


def latest_flash(effort: str = "low", models: list[tuple[str, str]] | None = None) -> str | None:
    """Newest Gemini Flash offered by agy, at the requested effort when that variant exists."""
    found = []
    for model_id, _ in models if models is not None else list_models():
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-flash(?:-(low|medium|high))?", model_id)
        if m:
            version = tuple(int(x) for x in m.group(1).split("."))
            found.append((version, m.group(2) == effort, model_id))
    return max(found)[2] if found else None
