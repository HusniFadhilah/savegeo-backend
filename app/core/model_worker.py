"""Small, credential-free environment for optional ML worker processes."""
from __future__ import annotations

import os
from pathlib import Path

_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
    "VIRTUAL_ENV", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES",
)


def model_worker_environment(cache_root: Path) -> dict[str, str]:
    """Return only runtime variables; never pass API or database credentials.

    The model worker receives local file paths as arguments and does not need
    the API process's database, provider, signing, or proxy secrets.
    """
    cache_root = cache_root.resolve()
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if os.environ.get(key)}
    env.update({
        "MPLCONFIGDIR": str(cache_root / "matplotlib"),
        "HF_HOME": str(cache_root / "huggingface"),
        "HF_HUB_ETAG_TIMEOUT": "30",
        "HF_HUB_DOWNLOAD_TIMEOUT": "60",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TOKENIZERS_PARALLELISM": "false",
    })
    return env
