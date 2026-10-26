"""The built frontend, served by the API process itself (the installable release).

Development runs Vite in front of this server (it proxies /api/*). An install
from a release has no Vite: `python3 scripts/api_server.py --serve-dist` serves
the `npm run build` output from the same origin as the API, so one process on
one port is the whole app.
"""
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIST_DIR = os.path.join(REPO_ROOT, "dist")


def mount_frontend(app: FastAPI, dist_dir: str) -> None:
    """Serve files from `dist_dir`, and index.html for any other non-API path (the app routes in the browser).

    Registered after every API route: a catch-all registered first would shadow them.
    """
    root = os.path.realpath(dist_dir)
    index = os.path.join(root, "index.html")
    if not os.path.isfile(index):
        raise SystemExit(f"--serve-dist: no index.html in {root}. Run `npm run build` first, or install a release.")

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str, request: Request):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        target = os.path.realpath(os.path.join(root, path))
        # Never outside the build directory (a "../" path).
        if path and target.startswith(root + os.sep) and os.path.isfile(target):
            # Hashed assets never change; the shell and the service worker must be re-read.
            cache = "public, max-age=31536000, immutable" if "/assets/" in target else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
