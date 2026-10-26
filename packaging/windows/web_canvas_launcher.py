"""Web Canvas for Windows: the API server serving the built app, and the browser opened on it.

Frozen by PyInstaller into WebCanvas.exe (.github/workflows/windows-installer.yml) and installed by
web_canvas.iss. Everything the app writes lives in the user's profile, never beside the program:
  %LOCALAPPDATA%\\WebCanvas\\storage   books, users, sessions (--storage-dir)
  %LOCALAPPDATA%\\WebCanvas\\db        the metadata DB, its key, the step journal (WEB_CANVAS_DB_DIR)
The window that opens is the server: closing it stops Web Canvas.

  WebCanvas.exe [--no-browser]        WEB_CANVAS_PORT picks the port (else the first free one from 3000)
"""
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser


def _bundle_dir() -> str:
    """Where dist/ and the server modules are: PyInstaller's unpack dir, or the repository when run from source."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _data_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "WebCanvas")


def _free_port(start: int = 3000) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("Web Canvas: no free port between 3000 and 3049.")


def _open_when_ready(url: str) -> None:
    for _ in range(120):
        try:
            with urllib.request.urlopen(url + "api/auth/session", timeout=2):
                break
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.5)
    webbrowser.open(url)


def main() -> None:
    no_browser = "--no-browser" in sys.argv
    data = _data_dir()
    os.makedirs(data, exist_ok=True)
    # Before the server modules are imported: server_db reads it at import.
    os.environ.setdefault("WEB_CANVAS_DB_DIR", os.path.join(data, "db"))
    port = int(os.environ.get("WEB_CANVAS_PORT") or _free_port())
    bundle = _bundle_dir()
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, os.path.join(bundle, "scripts"))
    sys.argv = [sys.argv[0], "--storage-dir", os.path.join(data, "storage"), "--host", "127.0.0.1",
                "--port", str(port), "--serve-dist", os.path.join(bundle, "dist")]
    url = f"http://127.0.0.1:{port}/"
    print(f"Web Canvas: {url}  (data in {data}; close this window to stop)")
    if not no_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    import api_server  # parses sys.argv (server_config) as it loads
    api_server.main()


if __name__ == "__main__":
    main()
