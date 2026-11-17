"""File helpers. Job files live under data/jobs/<job_id>/; the DB stores paths relative to data/."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from network.config import Settings


def job_dir(settings: Settings, job_id: int) -> Path:
    d = settings.jobs_dir / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def rel_to_data(settings: Settings, path: Path) -> str:
    return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()


def from_data(settings: Settings, rel_path: str) -> Path:
    return settings.data_dir / Path(rel_path)


def write_json_atomic(path: Path, data: Any) -> None:
    """Write to a temp file, then rename, so a crash never leaves half a file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
