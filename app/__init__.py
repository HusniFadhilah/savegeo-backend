"""SaveGeo application package."""
import sys

# Windows native ML wheels must load ORT before LightGBM/OpenMP. Importing it
# later can crash the process before Python can report an exception.
if sys.platform == "win32":
    import onnxruntime  # noqa: F401
