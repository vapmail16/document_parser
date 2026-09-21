import json

import httpx
from PIL import Image

from credit_extract.config import ModelSpec, Settings
from credit_extract.contracts import PageEnvelope
from credit_extract.gateway import OpenAICompatibleAdapter

ENVELOPE = {"blocks": [], "fields": [{"name": "Maturity Date", "raw_value": "October 28, 2027",
                                      "source_text": "means October 28, 2027", "confidence": 0.9}],
            "tables": [], "parties": []}


def spec(style="json_schema") -> ModelSpec:
    return ModelSpec(role="onprem", provider="openai_compatible", model_name="Qwen/Qwen2.5-VL-7B-Instruct",
                     api_base="http://gpu:8000/v1", prod_allowed=True, supports_sampling_params=True,
                     structured_output=True, structured_output_style=style, max_output_tokens=4096,
                     image={"max_long_edge_px": 1280}, params={"temperature": 0})


def adapter(handler, style="json_schema"):
    client = httpx.Client(base_url="http://gpu:8000/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatibleAdapter(Settings(), "onprem", spec(style), client=client)


def reply(content, finish="stop"):
    return httpx.Response(200, json={"model": "qwen", "choices": [{"message": {"content": content},
                                                                   "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 900, "completion_tokens": 120}})


def test_sends_image_and_schema_and_parses_envelope():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return reply(json.dumps(ENVELOPE))

    r = adapter(handler).extract([Image.new("L", (100, 100), 255)], "extract")
    assert r.ok and r.envelope.fields[0].raw_value == "October 28, 2027"
    parts = seen["messages"][0]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen["response_format"]["json_schema"]["schema"] == PageEnvelope.model_json_schema()
    assert seen["temperature"] == 0 and r.usage.input_tokens == 900 and r.usage.cost_usd is None


def test_vllm_guided_json_and_prompt_only_styles():
    seen = {}

    def handler(req):
        seen.clear()
        seen.update(json.loads(req.content))
        return reply("```json\n" + json.dumps(ENVELOPE) + "\n```")

    assert adapter(handler, "vllm_guided_json").extract([], "x").ok and "guided_json" in seen
    r = adapter(handler, "prompt_only").extract([], "x")
    assert r.ok and "matching this JSON schema" in seen["messages"][0]["content"][-1]["text"]


def test_typed_failures():
    assert adapter(lambda req: reply("{", finish="length")).extract([], "x").failure == "truncated"
    assert adapter(lambda req: reply("I think the date is October")).extract([], "x").failure == "invalid_output"
    assert adapter(lambda req: httpx.Response(400, text="bad")).extract([], "x").failure == "api_error"
