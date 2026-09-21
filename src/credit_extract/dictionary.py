"""Field dictionary snapshot store (spec §9).

Snapshots are immutable files dictionary/snapshots/v<NNNN>.json. A run pins one version;
runs never mutate a snapshot. Approved proposals are applied by writing the next version.
"""

import re
from pathlib import Path

from .contracts import DictionarySnapshot

_SNAPSHOT_RE = re.compile(r"^v(\d{4})\.json$")


def snapshot_path(dictionary_dir: Path, version: int) -> Path:
    return dictionary_dir / f"v{version:04d}.json"


def list_versions(dictionary_dir: Path) -> list[int]:
    return sorted(int(m.group(1)) for p in dictionary_dir.glob("v*.json") if (m := _SNAPSHOT_RE.match(p.name)))


def load_snapshot(dictionary_dir: Path, version: int | None = None) -> DictionarySnapshot:
    """Load a snapshot; version=None means the latest."""
    versions = list_versions(dictionary_dir)
    if not versions:
        raise FileNotFoundError(f"no dictionary snapshots under {dictionary_dir}")
    version = versions[-1] if version is None else version
    path = snapshot_path(dictionary_dir, version)
    if not path.exists():
        raise FileNotFoundError(f"dictionary snapshot v{version} not found; available: {versions}")
    snap = DictionarySnapshot.model_validate_json(path.read_text())
    if snap.version != version:
        raise ValueError(f"{path.name} declares version {snap.version}")
    return snap


def save_snapshot(dictionary_dir: Path, snapshot: DictionarySnapshot) -> Path:
    """Write a new snapshot. Existing versions are never overwritten."""
    versions = list_versions(dictionary_dir)
    expected = (versions[-1] + 1) if versions else 1
    if snapshot.version != expected:
        raise ValueError(f"next snapshot must be v{expected}, got v{snapshot.version}")
    if versions and snapshot.parent_version != versions[-1]:
        raise ValueError(f"snapshot v{snapshot.version} must declare parent_version={versions[-1]}")
    path = snapshot_path(dictionary_dir, snapshot.version)
    dictionary_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:  # "x": fail rather than overwrite
        f.write(snapshot.model_dump_json(indent=2, exclude_none=True) + "\n")
    return path
