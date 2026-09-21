import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from credit_extract.cli import app
from credit_extract.config import get_settings

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch):
    monkeypatch.setenv("CAE_ROOT", str(REPO))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_models_lists_registry():
    result = runner.invoke(app, ["models"])
    assert result.exit_code == 0, result.output
    assert "frontier" in result.output and "onprem_candidate" in result.output


def test_dict_list_core():
    result = runner.invoke(app, ["dict", "list", "--core"])
    assert result.exit_code == 0, result.output
    assert "applicable_margin" in result.output
    assert "guarantor" not in result.output  # not core


def test_dict_show_unknown_entry_fails():
    assert runner.invoke(app, ["dict", "show", "nope"]).exit_code == 1


def test_extract_refuses_a_disabled_model():
    result = runner.invoke(app, ["extract", str(REPO / "README.md"), "--document-id", "d", "--version-label", "v",
                                 "--model", "onprem_candidate"])
    assert result.exit_code != 0 and "disabled" in str(result.exception)


def test_every_documented_command_exists():
    # Guards against a command silently disappearing during an edit of cli.py.
    readme = (REPO / "README.md").read_text() + (REPO / "WORKPLACE.md").read_text()
    documented = set(re.findall(r"\bcae ([a-z][a-z0-9-]+)", readme))
    help_text = runner.invoke(app, ["--help"], terminal_width=200).output
    missing = [c for c in sorted(documented) if f" {c} " not in help_text and f"│ {c}" not in help_text]
    assert not missing, f"documented but not in the CLI: {missing}"
