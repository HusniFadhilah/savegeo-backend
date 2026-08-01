"""
Model Registry for managing multiple trained models
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ModelRegistry:
    """
    Manage multiple trained models with versioning
    """

    def __init__(self, models_dir: str = None):
        """Initialize model registry"""
        if models_dir:
            self.models_dir = Path(models_dir)
        else:
            from app.core.config import get_settings

            self.models_dir = get_settings().model_path
        self.models_dir.mkdir(parents=True, exist_ok=True)

        self.registry_file = self.models_dir / "registry.json"
        
        # ✅ FIX: Load registry DULU, BARU migrate
        self.registry = self._load_registry()
        self._migrate_registry_paths()  # Sekarang self.registry sudah ada

    def _load_registry(self) -> Dict:
        """Load registry from file"""
        if self.registry_file.exists():
            try:
                with open(self.registry_file, "r") as f:
                    data = json.load(f)
                    logger.info(f"✓ Registry loaded: {len(data.get('models', {}))} models")
                    return data
            except json.JSONDecodeError:
                logger.warning("Registry corrupted, reinitializing")
        
        logger.info("Creating new registry")
        return {"models": {}, "default": None}

    def _save_registry(self):
        """Save registry to file"""
        with open(self.registry_file, "w") as f:
            json.dump(self.registry, f, indent=2)

    def _migrate_registry_paths(self):
        """
        Migrate old absolute paths to portable relative paths
        ✅ Now called AFTER self.registry is initialized
        """
        changed = False
        
        for name, info in self.registry.get("models", {}).items():
            raw = info.get("path", "")
            
            # Extract just the filename
            stem = Path(raw.replace("\\", "/")).name
            
            if raw != stem:
                info["path"] = stem
                changed = True
                logger.info(f"  Migrated path for {name}: {raw} → {stem}")
        
        if changed:
            self._save_registry()
            logger.info("✓ Registry paths migrated to portable format")

    def register_model(
        self,
        model_name: str,
        model_path: str,
        metadata: Dict[str, Any],
        set_as_default: bool = False
    ):
        """
        Register a trained model
        
        Args:
            model_name: Unique name for the model
            model_path: Path to model file
            metadata: Model metadata
            set_as_default: Whether to set as default model
        """
        # Save only the filename (portable)
        stem = Path(model_path).name
        stem = stem.replace("\\", "/").split("/")[-1]

        self.registry["models"][model_name] = {
            "path": stem,
            "metadata": metadata,
            "registered_at": datetime.now().isoformat()
        }

        if set_as_default or self.registry["default"] is None:
            self.registry["default"] = model_name
            logger.info(f"✓ Set '{model_name}' as default model")

        self._save_registry()
        logger.info(f"✓ Registered model: {model_name}")

    def list_models(self) -> List[Dict]:
        """List all registered models"""
        models = []
        
        for name, info in self.registry["models"].items():
            models.append({
                "name": name,
                "path": str((self.models_dir / info["path"]).resolve()),
                "is_default": name == self.registry["default"],
                "registered_at": info["registered_at"],
                "algorithm": info["metadata"].get("algorithm"),
                "rmse": info["metadata"].get("cv_metrics", {}).get("rmse_mean"),
                "r2": info["metadata"].get("cv_metrics", {}).get("r2_mean"),
            })
        
        return models

    def get_model_path(self, model_name: Optional[str] = None) -> str:
        """
        Get full path to a model file
        
        Args:
            model_name: Name of model (uses default if None)
            
        Returns:
            Full path to model file
        """
        if model_name is None:
            model_name = self.registry['default']
            if model_name is None:
                raise ValueError("No default model set")

        if model_name not in self.registry['models']:
            available = list(self.registry['models'].keys())
            raise ValueError(
                f"Model '{model_name}' not found in registry. "
                f"Available models: {available}"
            )

        # Get relative path from registry
        raw = self.registry['models'][model_name]['path']

        # Normalize separators
        raw_norm = raw.replace("\\", "/")

        # Extract filename only
        stem = Path(raw_norm).name

        # Combine with models directory
        full = (self.models_dir / stem).resolve()

        return str(full)

    def get_model_info(self, model_name: Optional[str] = None) -> Dict:
        """
        Get metadata for a model
        
        Args:
            model_name: Name of model (uses default if None)
            
        Returns:
            Model metadata dictionary
        """
        if model_name is None:
            model_name = self.registry["default"]
            if model_name is None:
                raise ValueError("No default model set")

        if model_name not in self.registry["models"]:
            available = list(self.registry['models'].keys())
            raise ValueError(
                f"Model '{model_name}' not found in registry. "
                f"Available models: {available}"
            )

        return self.registry["models"][model_name]["metadata"]

    def set_default(self, model_name: str):
        """
        Set a model as default
        
        Args:
            model_name: Name of model to set as default
        """
        if model_name not in self.registry["models"]:
            raise ValueError(f"Model '{model_name}' not found")

        self.registry["default"] = model_name
        self._save_registry()
        logger.info(f"✓ Set '{model_name}' as default model")
    
    def delete_model(self, model_name: str):
        """
        Delete a model from registry
        
        Args:
            model_name: Name of model to delete
        """
        if model_name not in self.registry["models"]:
            raise ValueError(f"Model '{model_name}' not found")
        
        # Get path and delete file
        try:
            model_path = Path(self.get_model_path(model_name))
            if model_path.exists():
                model_path.unlink()
                logger.info(f"✓ Deleted model file: {model_path.name}")
        except Exception as e:
            logger.warning(f"Could not delete model file: {e}")
        
        # Remove from registry
        del self.registry["models"][model_name]
        
        # Clear default if this was the default
        if self.registry["default"] == model_name:
            self.registry["default"] = None
            logger.info("✓ Cleared default model")
        
        self._save_registry()
        logger.info(f"✓ Model removed from registry: {model_name}")