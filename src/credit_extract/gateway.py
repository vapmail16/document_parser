"""Model gateway (spec §4, §18): one extract() seam in front of every model.

Callers pass images + prompt + a models.yaml key; the adapter for that model's provider
does the rest. No stage knows which model it is talking to.

A refusal, a truncation (max_tokens) or output that fails the envelope schema is returned
as a typed failure, never as a partial result — the caller's retry policy decides (spec §8).
"""

import base64
import json
import os
import io
import time
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from PIL import Image
from pydantic import BaseModel

from .config import ModelSpec, Settings
from .contracts import PageEnvelope

MAX_IMAGE_BYTES = 4_500_000  # stay under the API's per-image limit

# blocked: the provider's output filter rejected the response (HTTP 400). Not retryable; the page goes
# to review. Seen on benign legal text (a "Defaulting Lender" definition) — report request ids upstream.
Failure = Literal["refusal", "blocked", "truncated", "invalid_output", "api_error"]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class ModelResponse(BaseModel):
    model_key: str
    served_model: str | None = None
    stop_reason: str | None = None
    envelope: PageEnvelope | None = None
    raw_text: str | None = None  # kept for audit, also on failure
    failure: Failure | None = None
    detail: str | None = None
    usage: Usage = Usage()
    latency_s: float = 0.0
    request_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and self.envelope is not None


def encode_image(img: Image.Image) -> tuple[str, bytes]:
    """PNG (lossless) unless too large — noisy scans don't compress — then high-quality JPEG."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= MAX_IMAGE_BYTES:
        return "image/png", buf.getvalue()
    for quality in (92, 85, 75):
        buf = io.BytesIO()
        img.convert("L" if img.mode in ("L", "1") else "RGB").save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return "image/jpeg", buf.getvalue()
    raise ValueError("image too large even as JPEG q75; tile more finely")


def _cost(spec: ModelSpec, usage: Usage) -> float | None:
    if spec.price_in_per_mtok is None or spec.price_out_per_mtok is None:
        return None
    return (usage.input_tokens * spec.price_in_per_mtok + usage.output_tokens * spec.price_out_per_mtok) / 1e6


def _api_key(settings: Settings, spec: ModelSpec) -> str | None:
    if not spec.api_key_env:
        return None
    return os.environ.get(spec.api_key_env) or dotenv_values(settings.path(Path(".env"))).get(spec.api_key_env)


class AnthropicAdapter:
    def __init__(self, settings: Settings, key: str, spec: ModelSpec):
        import anthropic

        self._anthropic = anthropic
        api_key = _api_key(settings, spec)
        if not api_key:
            raise RuntimeError(f"{spec.api_key_env} is not set (environment or .env) for model {key!r}")
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=600)
        self.key, self.spec = key, spec

    def extract(self, images: list[Image.Image], prompt: str) -> ModelResponse:
        content = []
        for img in images:
            media_type, data = encode_image(img)
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()}})
        content.append({"type": "text", "text": prompt})
        result, parsed = self._call(content, PageEnvelope)
        result.envelope = parsed
        return result

    def complete(self, prompt: str, output_model: type[BaseModel]) -> tuple[ModelResponse, BaseModel | None]:
        """Text-only call with a structured output (enrichment). Same failure typing as extract()."""
        return self._call([{"type": "text", "text": prompt}], output_model)

    def _call(self, content: list[dict], output_model: type[BaseModel]) -> tuple[ModelResponse, BaseModel | None]:
        output_config = {k: v for k, v in self.spec.params.items() if k in ("effort",)}
        t0 = time.monotonic()
        result = ModelResponse(model_key=self.key)
        try:
            msg = self.client.messages.parse(
                model=self.spec.model_name,
                max_tokens=self.spec.max_output_tokens,
                messages=[{"role": "user", "content": content}],
                output_format=output_model,
                **({"output_config": output_config} if output_config else {}),
            )
            result.request_id = getattr(msg, "_request_id", None)
        except self._anthropic.APIStatusError as e:
            blocked = e.status_code == 400 and "content filtering" in str(e.message).casefold()
            result.failure = "blocked" if blocked else "api_error"
            result.detail = f"HTTP {e.status_code}: {e.message}"
            result.request_id = e.response.headers.get("request-id")
            result.latency_s = time.monotonic() - t0
            return result, None
        except self._anthropic.APIConnectionError as e:
            result.failure, result.detail = "api_error", f"connection: {e}"
            result.latency_s = time.monotonic() - t0
            return result, None
        except ValueError as e:  # SDK could not validate the output against the schema
            result.failure, result.detail = "invalid_output", str(e)[:2000]
            result.latency_s = time.monotonic() - t0
            return result, None

        result.latency_s = time.monotonic() - t0
        result.served_model, result.stop_reason = msg.model, msg.stop_reason
        result.raw_text = "".join(b.text for b in msg.content if b.type == "text") or None
        result.usage = Usage(input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
        result.usage.cost_usd = _cost(self.spec, result.usage)

        # Check stop_reason before trusting content.
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            result.failure, result.detail = "refusal", str(getattr(details, "category", None) or "refused")
        elif msg.stop_reason == "max_tokens":
            result.failure, result.detail = "truncated", f"hit max_tokens={self.spec.max_output_tokens}"
        elif msg.parsed_output is None:
            result.failure, result.detail = "invalid_output", "no parsed output"
        else:
            return result, msg.parsed_output
        return result, None


class OpenAICompatibleAdapter:
    """On-prem models served by vLLM or Ollama through their OpenAI-compatible /v1/chat/completions.

    Same contract as AnthropicAdapter: typed failures, never a partial result. No SDK — plain HTTP, so
    the production install carries no provider library at all.
    """

    def __init__(self, settings: Settings, key: str, spec: ModelSpec, client: "httpx.Client | None" = None):
        import httpx

        if not spec.api_base:
            raise RuntimeError(f"model {key!r} needs api_base (e.g. http://gpu-host:8000/v1)")
        headers = {}
        if spec.api_key_env and (token := _api_key(settings, spec)):
            headers["Authorization"] = f"Bearer {token}"
        self._httpx = httpx
        self.client = client or httpx.Client(base_url=spec.api_base.rstrip("/"), headers=headers, timeout=900)
        self.key, self.spec = key, spec

    def extract(self, images: list[Image.Image], prompt: str) -> ModelResponse:
        content = []
        for img in images:
            media_type, data = encode_image(img)
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{base64.standard_b64encode(data).decode()}"}})
        content.append({"type": "text", "text": prompt})
        result, parsed = self._call(content, PageEnvelope)
        result.envelope = parsed
        return result

    def complete(self, prompt: str, output_model: type[BaseModel]) -> tuple[ModelResponse, BaseModel | None]:
        return self._call([{"type": "text", "text": prompt}], output_model)

    def _call(self, content: list[dict], output_model: type[BaseModel]) -> tuple[ModelResponse, BaseModel | None]:
        schema = output_model.model_json_schema()
        body: dict = {"model": self.spec.model_name, "max_tokens": self.spec.max_output_tokens,
                      "messages": [{"role": "user", "content": content}],
                      **{k: v for k, v in self.spec.params.items() if k not in ("effort",)}}
        style = self.spec.structured_output_style
        if style == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": output_model.__name__, "schema": schema, "strict": True}}
        elif style == "vllm_guided_json":
            body["guided_json"] = schema
        else:
            content[-1]["text"] += ("\n\nReply with a single JSON object and nothing else, matching this JSON schema:\n"
                                    + json.dumps(schema))
        t0 = time.monotonic()
        result = ModelResponse(model_key=self.key)
        resp = None
        for attempt in range(3):  # transient server errors: back off and retry
            try:
                resp = self.client.post("/chat/completions", json=body)
            except self._httpx.HTTPError as e:
                result.failure, result.detail = "api_error", f"connection: {e}"
                time.sleep(2 ** attempt)
                continue
            if resp.status_code < 500:
                break
            time.sleep(2 ** attempt)
        result.latency_s = time.monotonic() - t0
        if resp is None:
            return result, None
        result.request_id = resp.headers.get("x-request-id")
        if resp.status_code >= 400:
            result.failure, result.detail = "api_error", f"HTTP {resp.status_code}: {resp.text[:500]}"
            return result, None
        result.failure = result.detail = None
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        result.served_model = data.get("model")
        result.stop_reason = choice.get("finish_reason")
        result.raw_text = msg.get("content")
        result.usage = Usage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))
        result.usage.cost_usd = _cost(self.spec, result.usage)
        if result.stop_reason == "length":
            result.failure, result.detail = "truncated", f"hit max_tokens={self.spec.max_output_tokens}"
            return result, None
        if msg.get("refusal"):
            result.failure, result.detail = "refusal", str(msg["refusal"])[:500]
            return result, None
        text = _strip_fences(result.raw_text or "")
        try:
            return result, output_model.model_validate_json(text)
        except ValueError as e:
            result.failure, result.detail = "invalid_output", str(e)[:2000]
            return result, None


def _strip_fences(text: str) -> str:
    """Small models often wrap JSON in ```json fences, or add a sentence before it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start >= 0 and end > start else t


def get_adapter(settings: Settings, key: str):
    spec = settings.model(key)  # enforces enabled + prod_allowed
    if spec.provider == "anthropic":
        return AnthropicAdapter(settings, key, spec)
    return OpenAICompatibleAdapter(settings, key, spec)


def extract(images: list[Image.Image], prompt: str, model_key: str, settings: Settings) -> ModelResponse:
    """The single model seam (spec §18)."""
    return get_adapter(settings, model_key).extract(images, prompt)
