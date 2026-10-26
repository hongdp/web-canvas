"""Server configuration: CLI arguments, storage paths, and shared helpers.

This module owns the command-line/env configuration and the small
filesystem/validation helpers shared by the other server_* modules.
It is imported with plain sibling imports (`import server_config`) because
scripts/ is not a package — running `python3 scripts/api_server.py` puts
this directory on sys.path.
"""

import os
import re
import json
import argparse
from fastapi import HTTPException

# Parse command line arguments
parser = argparse.ArgumentParser(description="Web Canvas API Server")
parser.add_argument("--storage-dir", default=os.getenv("VITE_STORAGE_DIR", "storage"), help="Path to storage folder")
parser.add_argument("--host", default="127.0.0.1", help="Host IP to listen on")
parser.add_argument("--port", type=int, default=3000, help="Port to listen on")
parser.add_argument("--serve-dist", nargs="?", const="", default=None, metavar="DIR",
                    help="Also serve the built frontend (default DIR: the repository's dist/), so one process is the whole app")
parser.add_argument("--ssl-certfile", default=None, help="Serve HTTPS with this certificate (PEM)")
parser.add_argument("--ssl-keyfile", default=None, help="…and this private key (PEM)")
args, unknown = parser.parse_known_args()

STORAGE_DIR = os.path.abspath(args.storage_dir)
if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)

CONTENT_DIR = os.path.join(STORAGE_DIR, "content")
if not os.path.exists(CONTENT_DIR):
    os.makedirs(CONTENT_DIR, exist_ok=True)

USERS_FILE = os.path.join(STORAGE_DIR, "users.json")
SESSIONS_FILE = os.path.join(STORAGE_DIR, "sessions.json")

# ── Helper to read/write json files safely ─────────────────────────────────────
def load_json_file(file_path: str) -> dict:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json_file(file_path: str, data: dict):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[API Server] Error saving file {file_path}: {e}")

# ── Input validation helpers ───────────────────────────────────────────────────
def sanitize_username(username: str) -> str:
    """Strict alphanumeric username validation."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", os.path.basename(username))
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid username pattern.")
    return safe

def sanitize_id(value: str, label: str = "id") -> str:
    """Strict alphanumeric ID validation."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", os.path.basename(value))
    if not safe:
        raise HTTPException(status_code=400, detail=f"Invalid {label} pattern.")
    return safe
