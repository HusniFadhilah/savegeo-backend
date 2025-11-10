"""
Model Registry for managing multiple trained models
"""

import json
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ModelRegistry:
    """
    Manage multiple trained models with versioning
    """
    
    def __init__(self, models_dir: str = 'saved_models'):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(exist_ok=True)
        self.registry_file = self.models_dir / 'registry.json'
        self.registry = self._load_registry()
    
    def _load_registry(self) -> Dict:
        """Load model registry"""
        if self.registry_file.exists():
            with open(self.registry_file, 'r') as f:
                return json.load(f)
        return {'models': {}, 'default': None}
    
    def _save_registry(self):
        """Save model registry"""
        with open(self.registry_file, 'w') as f:
            json.dump(self.registry, f, indent=2)
    
    def register_model(self, 
                       model_name: str, 
                       model_path: str,
                       metadata: Dict,
                       set_as_default: bool = False):
        """
        Register a trained model
        
        Args:
            model_name: Unique model identifier (e.g., 'carbon_v1_global')
            model_path: Path to model file (without extension)
            metadata: Model metadata
            set_as_default: Set this model as default for inference
        """
        self.registry['models'][model_name] = {
            'path': str(model_path),
            'metadata': metadata,
            'registered_at': datetime.now().isoformat()
        }
        
        if set_as_default or self.registry['default'] is None:
            self.registry['default'] = model_name
            logger.info(f"✓ Set '{model_name}' as default model")
        
        self._save_registry()
        logger.info(f"✓ Registered model: {model_name}")
    
    def list_models(self) -> List[Dict]:
        """List all registered models"""
        models = []
        for name, info in self.registry['models'].items():
            models.append({
                'name': name,
                'path': info['path'],
                'is_default': name == self.registry['default'],
                'registered_at': info['registered_at'],
                'algorithm': info['metadata'].get('algorithm'),
                'rmse': info['metadata'].get('cv_metrics', {}).get('rmse_mean'),
                'r2': info['metadata'].get('cv_metrics', {}).get('r2_mean')
            })
        return models
    
    def get_model_path(self, model_name: Optional[str] = None) -> str:
        """
        Get path to model file
        
        Args:
            model_name: Model identifier (uses default if None)
        
        Returns:
            Path to model file
        """
        if model_name is None:
            model_name = self.registry['default']
            if model_name is None:
                raise ValueError("No default model set")
        
        if model_name not in self.registry['models']:
            raise ValueError(f"Model '{model_name}' not found in registry")
        
        return self.registry['models'][model_name]['path']
    
    def get_model_info(self, model_name: Optional[str] = None) -> Dict:
        """Get model metadata"""
        if model_name is None:
            model_name = self.registry['default']
        
        if model_name not in self.registry['models']:
            raise ValueError(f"Model '{model_name}' not found")
        
        return self.registry['models'][model_name]['metadata']
    
    def set_default(self, model_name: str):
        """Set default model for inference"""
        if model_name not in self.registry['models']:
            raise ValueError(f"Model '{model_name}' not found")
        
        self.registry['default'] = model_name
        self._save_registry()
        logger.info(f"✓ Set '{model_name}' as default model")