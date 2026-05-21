#!/usr/bin/env python3
"""Boots the FastAPI server and opens the browser.

Running this with no arguments is the supported entry point — `python run.py`.

The runner listens on 127.0.0.1:8765 by default. Port 8000 is left for
StoryUI / other local OpenAI-compatible engines.
"""

from __future__ import annotations

import argparse
import threading
import time
import webbrowser

import uvicorn


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default=8765, type=int)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open a browser tab on startup.")
    p.add_argument("--reload", action="store_true",
                   help="Enable uvicorn reload (development).")
    return p.parse_args()


def _is_wsl() -> bool:
    """True if running under WSL (so we need to shell out to Windows)."""
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            txt = f.read().lower()
        return "microsoft" in txt or "wsl" in txt
    except OSError:
        return False


def open_browser_when_ready(url: str, *, delay: float = 0.8) -> None:
    """Open the URL in the user's default browser after a short delay.

    On WSL, webbrowser.open() falls through to xdg-open which fails because
    no Linux browser is installed. Detect WSL and shell out to cmd.exe so the
    URL opens in the user's Windows default browser."""
    import subprocess

    def _go():
        time.sleep(delay)
        if _is_wsl():
            try:
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass  # fall through to webbrowser as last resort
        try:
            webbrowser.open(url)
        except Exception:
            pass  # we already printed the URL; user can copy-paste

    threading.Thread(target=_go, daemon=True).start()


def main() -> None:
    args = parse_args()
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  One Night Werewolf runner")
    print(f"  ----------------------------")
    print(f"  Listening on {url}")
    print(f"  (Ctrl-C to stop)\n")
    if not args.no_browser:
        open_browser_when_ready(url)

    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
