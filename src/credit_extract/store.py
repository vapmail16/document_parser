"""Minimal persistence (spec §6, §20 milestone 2).

Layout under the store root:
  blobs/<aa>/<sha256>.pdf                   raw inputs, content-addressed, write-once
  pages/<sha256>/<dpi>dpi-<mode>/p0001.png  rasterised pages + manifest.json (derived cache)
  runs/<run_id>.json                        one record per run, write-once
"""

import hashlib
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .contracts import PageManifest, RunRecord, StageRecord


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


class Store:
    def __init__(self, root: Path):
        self.root = root

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def abs(self, rel: str) -> Path:
        return self.root / rel

    # --- raw inputs -------------------------------------------------------

    def blob_path(self, sha: str) -> Path:
        return self.root / "blobs" / sha[:2] / f"{sha}.pdf"

    def put_pdf(self, src: Path) -> str:
        """Store a PDF by content hash. Idempotent; returns the sha256."""
        sha = sha256_file(src)
        dest = self.blob_path(sha)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            shutil.copyfile(src, tmp)
            if sha256_file(tmp) != sha:
                tmp.unlink()
                raise IOError(f"hash mismatch copying {src}")
            tmp.rename(dest)
        return sha

    # --- rasterised pages -------------------------------------------------

    def pages_dir(self, sha: str, dpi: int, grayscale: bool) -> Path:
        return self.root / "pages" / sha / f"{dpi}dpi-{'gray' if grayscale else 'rgb'}"

    def load_manifest(self, sha: str, dpi: int, grayscale: bool) -> PageManifest | None:
        path = self.pages_dir(sha, dpi, grayscale) / "manifest.json"
        return PageManifest.model_validate_json(path.read_text()) if path.exists() else None

    def save_manifest(self, manifest: PageManifest) -> Path:
        # Written last, after every page image: its presence marks a complete render.
        path = self.pages_dir(manifest.source_sha256, manifest.dpi, manifest.grayscale) / "manifest.json"
        path.write_text(manifest.model_dump_json(indent=2) + "\n")
        return path

    # --- run records ------------------------------------------------------

    def save_run(self, run: RunRecord) -> Path:
        path = self.root / "runs" / f"{run.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as f:  # write-once
            f.write(run.model_dump_json(indent=2) + "\n")
        return path

    def load_run(self, run_id: str) -> RunRecord:
        """The run record with every stage appended after ingest, in order."""
        run = RunRecord.model_validate_json((self.root / "runs" / f"{run_id}.json").read_text())
        log = self.root / "runs" / f"{run_id}.stages.jsonl"
        if log.exists():
            extra = [StageRecord.model_validate_json(ln) for ln in log.read_text().splitlines() if ln.strip()]
            run = run.model_copy(update={"stages": run.stages + extra})
        return run

    def append_stage(self, run_id: str, stage: StageRecord) -> None:
        """Later stages append to a log; the ingest record itself stays write-once."""
        if not (self.root / "runs" / f"{run_id}.json").exists():
            raise FileNotFoundError(f"no run {run_id}")
        with (self.root / "runs" / f"{run_id}.stages.jsonl").open("a") as f:
            f.write(stage.model_dump_json() + "\n")

    def save_json(self, rel: str, model) -> Path:
        """Write a derived artefact (e.g. an OCR report) under the store root."""
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model.model_dump_json(indent=2) + "\n")
        return path
