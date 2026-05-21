"""Notes and gameskill file I/O.

Notes are per-game scratchpads. Gameskills are persistent per-player files,
append-mostly across games with periodic compression. Both are plain markdown
that the runner reads, injects into context, and overwrites with model
responses."""

from __future__ import annotations

from pathlib import Path


def game_dir(workspace_root: Path, game_id: str) -> Path:
    d = workspace_root / "games" / game_id
    (d / "notes").mkdir(parents=True, exist_ok=True)
    (d / "afterthoughts").mkdir(parents=True, exist_ok=True)
    return d


def notes_path(workspace_root: Path, game_id: str, nickname: str) -> Path:
    return game_dir(workspace_root, game_id) / "notes" / f"{nickname}.md"


def afterthought_path(workspace_root: Path, game_id: str, nickname: str) -> Path:
    return game_dir(workspace_root, game_id) / "afterthoughts" / f"{nickname}.md"


def gameskill_path(workspace_root: Path, nickname: str) -> Path:
    (workspace_root / "gameskills" / "_history").mkdir(parents=True, exist_ok=True)
    return workspace_root / "gameskills" / f"{nickname}.md"


def read_notes(workspace_root: Path, game_id: str, nickname: str) -> str:
    p = notes_path(workspace_root, game_id, nickname)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save_notes(workspace_root: Path, game_id: str, nickname: str, content: str) -> Path:
    """Overwrite the notes file with `content` (the full new content per spec).

    The runner ensures the "My goal" section is re-injected verbatim via the
    prompt template, so we don't enforce it on the file side."""
    p = notes_path(workspace_root, game_id, nickname)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def read_gameskill(workspace_root: Path, nickname: str) -> str:
    p = gameskill_path(workspace_root, nickname)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save_gameskill(workspace_root: Path, nickname: str, content: str) -> Path:
    p = gameskill_path(workspace_root, nickname)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def archive_gameskill(workspace_root: Path, nickname: str, game_id: str) -> Path | None:
    """Snapshot the current gameskill into _history/ before an update or
    compression pass overwrites it. Returns the archive path, or None if
    there was nothing to archive."""
    src = gameskill_path(workspace_root, nickname)
    if not src.exists():
        return None
    dst = workspace_root / "gameskills" / "_history" / f"{nickname}_{game_id}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
