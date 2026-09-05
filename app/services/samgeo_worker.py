"""Run the optional SamGeo dependency without changing the API's ML packages."""
import sys
import os
from pathlib import Path


def main():
    source, destination, checkpoint_dir = sys.argv[1:]
    os.environ.setdefault("MPLCONFIGDIR", str(Path(checkpoint_dir).parent / "matplotlib"))
    import torch
    from samgeo import SamGeo

    torch.set_num_threads(4)
    model = SamGeo(model_type="vit_b", automatic=True, checkpoint_dir=checkpoint_dir,
                   sam_kwargs={"points_per_side": 16, "points_per_batch": 16})
    model.generate(source, output=destination, unique=True)


if __name__ == "__main__":
    main()
