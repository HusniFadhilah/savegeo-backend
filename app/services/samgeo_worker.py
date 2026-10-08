"""Run the optional SamGeo dependency without changing the API's ML packages."""
import sys
import os
import gc
from pathlib import Path


def main():
    source, destination, checkpoint_dir = sys.argv[1:4]
    mode = sys.argv[4] if len(sys.argv) > 4 else "automatic"
    prompt = sys.argv[5] if len(sys.argv) > 5 else ""
    confidence = float(sys.argv[6]) if len(sys.argv) > 6 else 0.5
    os.environ.setdefault("MPLCONFIGDIR", str(Path(checkpoint_dir).parent / "matplotlib"))
    os.environ.setdefault("HF_HOME", str(Path(checkpoint_dir) / "huggingface"))
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    import torch
    from samgeo import SamGeo

    torch.set_num_threads(4)
    if mode == "prompt_based":
        # Use the Transformers implementation; avoid implicit pip installs.
        import inspect
        import numpy as np
        import rasterio
        from PIL import Image
        from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection
        from torchvision.ops import nms
        with rasterio.open(source) as dataset:
            rgb = dataset.read().transpose(1, 2, 0)
            profile = dataset.profile.copy()
        image = Image.fromarray(rgb)
        model_id = "IDEA-Research/grounding-dino-tiny"
        processor = AutoProcessor.from_pretrained(model_id)
        detector = AutoModelForZeroShotObjectDetection.from_pretrained(model_id, use_safetensors=True).eval()
        inputs = processor(images=image, text=prompt.rstrip(".")+".", return_tensors="pt")
        with torch.inference_mode():
            outputs = detector(**inputs)
        method = processor.post_process_grounded_object_detection
        threshold_key = "threshold" if "threshold" in inspect.signature(method).parameters else "box_threshold"
        result = method(outputs, inputs.input_ids, text_threshold=confidence,
                        target_sizes=[image.size[::-1]], **{threshold_key: confidence})[0]
        boxes = result["boxes"].detach().cpu()
        scores = result["scores"].detach().cpu()
        boxes = boxes[nms(boxes, scores, 0.5)] if len(boxes) else boxes
        if len(boxes) > 256:
            raise ValueError("Terlalu banyak kandidat; gunakan potongan AOI lebih kecil")
        del detector, outputs, inputs
        gc.collect()
        labels = np.zeros(rgb.shape[:2], dtype="int32")
        if len(boxes):
            model = SamGeo(model_type="vit_b", automatic=False, checkpoint_dir=checkpoint_dir)
            model.predictor.set_image(rgb)
            for index, box in enumerate(boxes):
                masks, _, _ = model.predictor.predict(box=box.numpy(), multimask_output=False)
                labels[masks[0]] = index+1
        profile.update(count=1, dtype="int32")
        with rasterio.open(destination, "w", **profile) as dataset:
            dataset.write(labels, 1)
    else:
        model = SamGeo(model_type="vit_b", automatic=True, checkpoint_dir=checkpoint_dir,
                       sam_kwargs={"points_per_side": 16, "points_per_batch": 16})
        model.generate(source, output=destination, unique=True)


if __name__ == "__main__":
    main()
