"""Settings + models.yaml (spec §5, §18).

No model name may be hard-coded outside models.yaml. Code refers to models by their key.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SAMPLING_PARAMS = {"temperature", "top_p", "top_k"}


class ImageBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_long_edge_px: int = Field(gt=0)  # images above this are tiled (spec §8), not downscaled
    max_pixels: int | None = Field(default=None, gt=0)


class TilingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlap_frac: float = Field(default=0.10, ge=0.0, lt=0.5)
    max_tiles_per_page: int = Field(default=4, ge=1)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["frontier", "onprem", "baseline"]
    provider: Literal["anthropic", "openai_compatible"]  # adapter behind the single extract() seam
    model_name: str
    api_base: str | None = None
    api_key_env: str | None = None  # name of the env var holding the key; never the key itself
    enabled: bool = True
    prod_allowed: bool  # frontier/SaaS models must be False (spec §3)
    licence: str | None = None  # weights licence for open models — check per size
    supports_sampling_params: bool
    structured_output: bool
    # How the openai_compatible adapter asks for JSON:
    #   json_schema       OpenAI-style response_format (vLLM >= 0.6, recent Ollama)
    #   vllm_guided_json  vLLM's extra body parameter `guided_json` (older vLLM)
    #   prompt_only       schema described in the prompt, JSON parsed from the reply (weakest; last resort)
    structured_output_style: Literal["json_schema", "vllm_guided_json", "prompt_only"] = "json_schema"
    max_output_tokens: int = Field(gt=0)
    image: ImageBudget
    tiling: TilingConfig = Field(default_factory=TilingConfig)
    params: dict[str, Any] = {}  # passed through to the gateway call
    price_in_per_mtok: float | None = None  # USD, for cost/page reporting (spec §17)
    price_out_per_mtok: float | None = None
    concurrency: int = Field(default=4, ge=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ModelSpec":
        if self.role == "frontier" and self.prod_allowed:
            raise ValueError("frontier models can never be prod_allowed (spec §3)")
        if not self.supports_sampling_params and SAMPLING_PARAMS & self.params.keys():
            raise ValueError(
                f"{self.model_name} rejects sampling params; remove "
                f"{sorted(SAMPLING_PARAMS & self.params.keys())} from params"
            )
        return self


class ModelsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str
    eval_pair: tuple[str, str]  # (reference, candidate) for `eval` mode (spec §12)
    models: dict[str, ModelSpec]

    @model_validator(mode="after")
    def _refs_exist(self) -> "ModelsFile":
        for ref in (self.default_model, *self.eval_pair):
            if ref not in self.models:
                raise ValueError(f"models.yaml references unknown model key {ref!r}")
        return self


class Settings(BaseSettings):
    """Env vars prefixed CAE_ override defaults, e.g. CAE_ENVIRONMENT=prod."""

    model_config = SettingsConfigDict(env_prefix="CAE_", env_file=".env", extra="ignore")

    environment: Literal["poc", "prod"] = "poc"
    root: Path = Path(".")
    models_file: Path = Path("models.yaml")
    prompts_dir: Path = Path("prompts")
    dictionary_dir: Path = Path("dictionary/snapshots")
    store_dir: Path = Path("var/store")  # content-addressed blobs + run records (gitignored)
    rasterise_dpi: int = Field(default=300, ge=300)  # spec §6 Stage 1: 300 DPI minimum
    rasterise_grayscale: bool = True  # ~3x smaller page images; set False if colour stamps matter

    def path(self, p: Path) -> Path:
        return p if p.is_absolute() else self.root / p

    def load_models(self) -> ModelsFile:
        return load_models_file(self.path(self.models_file))

    def model(self, key: str) -> ModelSpec:
        models = self.load_models()
        if key not in models.models:
            raise KeyError(f"unknown model key {key!r}; known: {sorted(models.models)}")
        spec = models.models[key]
        if not spec.enabled:
            raise ValueError(f"model {key!r} is disabled in models.yaml")
        if self.environment == "prod" and not spec.prod_allowed:
            raise PermissionError(
                f"model {key!r} is not prod_allowed; production may only use self-hosted models (spec §3)"
            )
        return spec


def load_models_file(path: Path) -> ModelsFile:
    with path.open() as f:
        return ModelsFile.model_validate(yaml.safe_load(f))


@lru_cache
def get_settings() -> Settings:
    return Settings()
