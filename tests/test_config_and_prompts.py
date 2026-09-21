from pathlib import Path

import pytest
from pydantic import ValidationError

from credit_extract.config import ModelSpec, Settings, load_models_file
from credit_extract.prompts import PROMPT_NAMES, load_prompt

REPO = Path(__file__).resolve().parents[1]


def spec(**kw) -> dict:
    return dict(role="frontier", provider="anthropic", model_name="x", prod_allowed=False, supports_sampling_params=False,
                structured_output=True, max_output_tokens=1000, image={"max_long_edge_px": 1000}) | kw


def test_models_yaml_is_valid():
    cfg = load_models_file(REPO / "models.yaml")
    assert cfg.models[cfg.eval_pair[0]].role == "frontier"
    assert cfg.models[cfg.eval_pair[1]].role == "onprem"


def test_frontier_model_can_never_be_prod_allowed():
    with pytest.raises(ValidationError, match="never be prod_allowed"):
        ModelSpec(**spec(prod_allowed=True))


def test_sampling_params_rejected_where_unsupported():
    with pytest.raises(ValidationError, match="rejects sampling params"):
        ModelSpec(**spec(params={"temperature": 0}))


def test_prod_environment_refuses_frontier_models():
    settings = Settings(root=REPO, environment="prod")
    with pytest.raises(PermissionError, match="not prod_allowed"):
        settings.model("frontier")
    assert Settings(root=REPO, environment="poc").model("frontier").role == "frontier"


def test_dpi_floor():
    with pytest.raises(ValidationError):
        Settings(rasterise_dpi=200)


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_prompt_has_a_version_and_renders(name):
    p = load_prompt(REPO / "prompts", name)
    assert p.version.startswith("v") and len(p.sha256) == 64
    # Literal dollar amounts must be escaped as $$ or rendering raises.
    p.render(**{k: "x" for k in p.placeholders})


def test_prompt_render_requires_all_placeholders():
    p = load_prompt(REPO / "prompts", "pass2_schema_hint")
    assert p.placeholders == {"known_fields", "already_extracted"}
    with pytest.raises(KeyError, match="already_extracted"):
        p.render(known_fields="x")
    assert "maturity_date" in p.render(known_fields="maturity_date — ...", already_extracted="(none)")


def test_pass1_prompt_is_not_given_the_dictionary():
    # Spec §8: Pass 1 is open-ended; feeding it the field list defeats "extract everything".
    assert load_prompt(REPO / "prompts", "pass1_extraction").placeholders == set()
