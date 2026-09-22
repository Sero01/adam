"""File tools. They run inside the (root) runner process, so every path is
confined to the workspace after resolving symlinks — otherwise file_read would
be a way around the uid separation that keeps state.db away from the agent.
Anything outside the workspace is reachable via `shell`, as the agent user."""
from __future__ import annotations

import os
from pathlib import Path

READ_CAP = 32 * 1024
LIST_CAP = 500


class PathError(ValueError):
    pass


def resolve(workspace: Path, path: str) -> Path:
    ws = workspace.resolve()
    s = str(path).replace("\\", "/")
    if s == "/workspace" or s.startswith("/workspace/"):
        # the agent always sees /workspace; map it onto the real directory (differs in tests)
        p = ws / s[len("/workspace"):].lstrip("/")
    else:
        p = Path(path)
        if not p.is_absolute():
            p = ws / p
    real = p.resolve()
    if real != ws and ws not in real.parents:
        raise PathError(f"{path}: outside /workspace (use shell for other locations)")
    return real


def display(workspace: Path, real: Path) -> str:
    rel = real.relative_to(workspace.resolve())
    return "/workspace" + ("" if str(rel) == "." else "/" + rel.as_posix())


def _chown(p: Path, owner: tuple[int, int] | None) -> None:
    if owner and hasattr(os, "chown"):
        try:
            os.chown(p, *owner)
        except OSError:
            pass


def file_read(workspace: Path, path: str, offset: int = 0, limit: int = READ_CAP) -> dict:
    real = resolve(workspace, path)
    if not real.is_file():
        raise PathError(f"{path}: not a file")
    limit = max(1, min(int(limit), READ_CAP))
    offset = max(0, int(offset))
    size = real.stat().st_size
    with open(real, "rb") as fh:
        fh.seek(offset)
        data = fh.read(limit)
    if b"\x00" in data[:8192]:
        return {"path": display(workspace, real), "size": size, "binary": True,
                "content": "", "note": "binary file; inspect with shell (xxd, file, ...)"}
    end = offset + len(data)
    return {"path": display(workspace, real), "size": size, "offset": offset,
            "content": data.decode("utf-8", errors="replace"),
            "truncated": end < size, "next_offset": end if end < size else None}


def file_write(workspace: Path, path: str, content: str,
               owner: tuple[int, int] | None = None) -> dict:
    real = resolve(workspace, path)
    if real.is_dir():
        raise PathError(f"{path}: is a directory")
    ws = workspace.resolve()
    missing = [d for d in [real.parent, *real.parent.parents] if ws in d.parents and not d.exists()]
    real.parent.mkdir(parents=True, exist_ok=True)
    for d in missing:
        _chown(d, owner)
    real.write_text(content, encoding="utf-8", newline="")
    _chown(real, owner)
    return {"path": display(workspace, real), "bytes": len(content.encode("utf-8"))}


def file_list(workspace: Path, path: str = ".", depth: int = 1) -> dict:
    root = resolve(workspace, path)
    if not root.is_dir():
        raise PathError(f"{path}: not a directory")
    depth = max(1, min(int(depth), 5))
    entries: list[str] = []
    truncated = False

    def walk(d: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(d.iterdir(), key=lambda c: c.name)
        except OSError as e:
            entries.append(f"{display(workspace, d)}/ [unreadable: {e.strerror}]")
            return
        for c in children:
            if len(entries) >= LIST_CAP:
                truncated = True
                return
            try:
                if c.is_symlink():
                    entries.append(f"{'  ' * level}{c.name} -> {os.readlink(c)}")
                elif c.is_dir():
                    entries.append(f"{'  ' * level}{c.name}/")
                    if level + 1 < depth:
                        walk(c, level + 1)
                else:
                    entries.append(f"{'  ' * level}{c.name}  ({c.stat().st_size} B)")
            except OSError:
                entries.append(f"{'  ' * level}{c.name}  [stat failed]")

    walk(root, 0)
    return {"path": display(workspace, root), "entries": entries, "truncated": truncated}
