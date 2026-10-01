"""Run the optional SamGeo dependency without changing the API's ML packages."""
import sys
import os
from pathlib import Path


def main():
    source, destination, checkpoint_dir = sys.argv[1:4]
    mode = sys.argv[4] if len(sys.argv) > 4 else "automatic"
    prompt = sys.argv[5] if len(sys.argv) > 5 else ""
    confidence = float(sys.argv[6]) if len(sys.argv) > 6 else 0.5
    os.environ.setdefault("MPLCONFIGDIR", str(Path(checkpoint_dir).parent / "matplotlib"))
    import torch
    from samgeo import SamGeo

    torch.set_num_threads(4)
    if mode == "prompt_based":
        from samgeo.text_sam import LangSAM
        model = LangSAM()
        model.predict(source, text_prompt=prompt, box_threshold=confidence, text_threshold=confidence, output=destination)
    else:
        model = SamGeo(model_type="vit_b", automatic=True, checkpoint_dir=checkpoint_dir,
                       sam_kwargs={"points_per_side": 16, "points_per_batch": 16})
        model.generate(source, output=destination, unique=True)


if __name__ == "__main__":
    main()
