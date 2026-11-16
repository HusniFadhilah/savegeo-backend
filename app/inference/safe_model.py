"""Data-only regression persistence; never deserialize Python objects at runtime."""
from __future__ import annotations

from pathlib import Path

import numpy as np
# Load ONNX Runtime before optional ML libraries. Some Windows OpenMP builds
# otherwise collide with LightGBM's native runtime during lazy DLL loading.
import onnxruntime as ort


def validate_onnx(raw: bytes):
    import onnx
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("Model exceeds 128 MiB")
    graph = onnx.load_model_from_string(raw)
    if graph.functions or len(graph.graph.node) > 200_000:
        raise ValueError("Model graph exceeds the supported execution contract")
    for tensor in graph.graph.initializer:
        if tensor.external_data or tensor.data_location == onnx.TensorProto.EXTERNAL:
            raise ValueError("External model data is not allowed")
    for node in graph.graph.node:
        if node.domain not in {"", "ai.onnx", "ai.onnx.ml"}:
            raise ValueError("Custom model operators are not allowed")
        for attribute in node.attribute:
            if attribute.type in {onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS}:
                raise ValueError("Nested executable graphs are not supported")
            if attribute.type == onnx.AttributeProto.TENSOR and attribute.t.external_data:
                raise ValueError("External model data is not allowed")
    onnx.checker.check_model(graph)
    return graph


class OnnxRegressor:
    def __init__(self, raw: bytes, metadata: dict):
        validate_onnx(raw)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(raw, sess_options=options, providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        if len(inputs) != 1 or inputs[0].type not in {"tensor(float)", "tensor(double)"}:
            raise ValueError("Model must accept one numeric feature matrix")
        self.input = inputs[0]
        self.dtype = np.float64 if self.input.type == "tensor(double)" else np.float32
        linear = metadata.get("linear_parameters")
        if isinstance(linear, dict):
            self.coef_ = np.asarray(linear["coef"], dtype=float)
            self.intercept_ = float(linear["intercept"])

    def predict(self, X):
        values = np.asarray(X, dtype=self.dtype)
        if values.ndim != 2 or values.size > 20_000_000 or not np.isfinite(values).all():
            raise ValueError("Expected a finite bounded feature matrix")
        output = np.asarray(self.session.run(None, {self.input.name: values})[0]).reshape(-1)
        if output.size != values.shape[0] or not np.isfinite(output).all():
            raise ValueError("Model returned invalid predictions")
        return output


def export_estimator(model, path: Path, n_features: int) -> None:
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType
    from sklearn.ensemble import HistGradientBoostingRegressor
    from skl2onnx.operator_converters.random_forest import convert_sklearn_random_forest_regressor_converter

    def convert_hist(scope, operator, container):
        # skl2onnx emits bools for an ONNX INTS attribute. Recent protobuf
        # versions correctly reject these. Normalize this one attribute on
        # the conversion-local container, without changing library globals.
        class ContainerAdapter:
            def __getattr__(self, name):
                return getattr(container, name)
            def add_node(self, *args, **kwargs):
                key = "nodes_missing_value_tracks_true"
                if key in kwargs:
                    kwargs[key] = [int(value) for value in kwargs[key]]
                return container.add_node(*args, **kwargs)
        return convert_sklearn_random_forest_regressor_converter(scope, operator, ContainerAdapter())

    if type(model).__module__.startswith(('lightgbm.', 'xgboost.')):
        from onnxmltools import convert_lightgbm, convert_xgboost
        from onnxmltools.convert.common.data_types import FloatTensorType as ExternalFloatTensorType
        converter = convert_lightgbm if type(model).__module__.startswith('lightgbm.') else convert_xgboost
        graph = converter(model, initial_types=[('features', ExternalFloatTensorType([None, n_features]))], target_opset=15)
    else:
        graph = convert_sklearn(model, initial_types=[("features", FloatTensorType([None, n_features]))],
            target_opset=17, custom_conversion_functions={HistGradientBoostingRegressor: convert_hist})
    raw = graph.SerializeToString()
    validate_onnx(raw)
    # Verify that the runtime accepts the result before replacing an existing export.
    OnnxRegressor(raw, {})
    temporary = path.with_suffix(".onnx.tmp")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
