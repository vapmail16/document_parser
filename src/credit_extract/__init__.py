"""Credit agreement extraction & versioning pipeline (POC)."""

import os

__version__ = "0.1.0"

# Offline posture (spec §5). Set before any stage imports litellm / sentence-transformers,
# both of which otherwise reach the network on import or first use. setdefault so an
# operator can still override explicitly.
OFFLINE_ENV = {
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}
for _key, _value in OFFLINE_ENV.items():
    os.environ.setdefault(_key, _value)
