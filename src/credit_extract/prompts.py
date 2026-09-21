"""Versioned prompt templates (spec §8).

Layout: prompts/<name>/v<N>.md. Templates use string.Template ($var) placeholders so JSON
examples in the prompt text need no escaping. A run records the (name -> version) it used
in Provenance.prompt_versions, plus the template hash for audit.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

PROMPT_NAMES = ("pass1_extraction", "pass2_schema_hint", "enrichment")
_VERSION_RE = re.compile(r"^v(\d+)\.md$")


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str  # "v1"
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def placeholders(self) -> set[str]:
        return {m.group("named") or m.group("braced")
                for m in Template.pattern.finditer(self.text)
                if m.group("named") or m.group("braced")}

    def render(self, **values: str) -> str:
        missing = self.placeholders - values.keys()
        if missing:
            raise KeyError(f"prompt {self.name}/{self.version} missing values for {sorted(missing)}")
        return Template(self.text).substitute(values)


def available_versions(prompts_dir: Path, name: str) -> list[str]:
    versions = []
    for p in (prompts_dir / name).glob("v*.md"):
        if m := _VERSION_RE.match(p.name):
            versions.append(int(m.group(1)))
    return [f"v{n}" for n in sorted(versions)]


def load_prompt(prompts_dir: Path, name: str, version: str | None = None) -> PromptTemplate:
    """Load a prompt; version=None means the latest."""
    versions = available_versions(prompts_dir, name)
    if not versions:
        raise FileNotFoundError(f"no versions of prompt {name!r} under {prompts_dir}")
    version = version or versions[-1]
    if version not in versions:
        raise FileNotFoundError(f"prompt {name!r} has no version {version!r}; available: {versions}")
    return PromptTemplate(name, version, (prompts_dir / name / f"{version}.md").read_text())
