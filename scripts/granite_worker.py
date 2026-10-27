"""Run IBM Granite inference in the isolated TerraTorch environment."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    input_dir, checkpoint_path, config_path, output_path, metadata_path, dataset_bands_json = sys.argv[1:7]
    try:
        import numpy as np
        import torch
        from terratorch.cli_tools import LightningInferenceModel

        model = LightningInferenceModel.from_config(
            config_path,
            checkpoint_path,
            json.loads(dataset_bands_json),
        )
        model.eval()
        with torch.inference_mode():
            results, filenames = model.inference_on_dir(input_dir)
        if len(results) != 1 or len(filenames) != 1:
            raise ValueError("TerraTorch did not return one prediction")
        prediction = results[0]
        if hasattr(prediction, "detach"):
            prediction = prediction.detach().cpu().numpy()
        np.save(output_path, np.asarray(prediction), allow_pickle=False)
        Path(metadata_path).write_text(
            json.dumps({"filenames": [str(filename) for filename in filenames]}),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # keep model/checkpoint internals out of API responses
        print(f"IBM Granite inference failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
