"""
Model Evaluation Module
Provides comprehensive evaluation metrics and visualization for carbon estimation models
"""

import numpy as np
import matplotlib.pyplot as plt
from typing import Dict, List, Optional, Tuple
import logging
from pathlib import Path

try:
    from sklearn.metrics import (
        mean_squared_error, 
        mean_absolute_error, 
        r2_score,
        mean_absolute_percentage_error
    )
    from sklearn.model_selection import cross_val_score, cross_val_predict
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("⚠ Warning: scikit-learn not available for model evaluation")

logger = logging.getLogger(__name__)


def evaluate_model(model, X_test: np.ndarray, y_test: np.ndarray, 
                   feature_names: Optional[List[str]] = None,
                   save_plots: bool = False,
                   output_dir: str = 'evaluation_results') -> Dict:
    """
    Comprehensive model evaluation
    
    Args:
        model: Trained model instance (CarbonEstimationModel or sklearn model)
        X_test: Test features
        y_test: Test target values
        feature_names: Names of features
        save_plots: Whether to save evaluation plots
        output_dir: Directory to save plots
    
    Returns:
        Dict with evaluation metrics and plots
    """
    
    if not SKLEARN_AVAILABLE:
        raise ImportError("scikit-learn is required for model evaluation")
    
    logger.info("=" * 60)
    logger.info("MODEL EVALUATION")
    logger.info("=" * 60)
    
    # Make predictions
    logger.info("Making predictions on test set...")
    y_pred = model.predict(X_test)
    
    # Calculate metrics
    metrics = calculate_metrics(y_test, y_pred)
    
    # Log metrics
    logger.info("\n--- Test Set Performance ---")
    logger.info(f"RMSE: {metrics['rmse']:.2f} Mg/ha")
    logger.info(f"MAE: {metrics['mae']:.2f} Mg/ha")
    logger.info(f"R² Score: {metrics['r2']:.4f}")
    logger.info(f"MAPE: {metrics['mape']:.2f}%")
    
    # Residual analysis
    residuals = calculate_residuals(y_test, y_pred)
    logger.info("\n--- Residual Analysis ---")
    logger.info(f"Mean Residual: {residuals['mean']:.2f} Mg/ha")
    logger.info(f"Std Residual: {residuals['std']:.2f} Mg/ha")
    logger.info(f"Max Overestimation: {residuals['max_positive']:.2f} Mg/ha")
    logger.info(f"Max Underestimation: {residuals['max_negative']:.2f} Mg/ha")
    
    # Create evaluation plots
    if save_plots:
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True, parents=True)
        
        logger.info(f"\n--- Generating Evaluation Plots ---")
        
        # Scatter plot: Actual vs Predicted
        plot_actual_vs_predicted(y_test, y_pred, metrics, 
                                 save_path=output_path / 'actual_vs_predicted.png')
        
        # Residual plot
        plot_residuals(y_test, y_pred, residuals,
                       save_path=output_path / 'residuals.png')
        
        # Error distribution
        plot_error_distribution(y_test, y_pred,
                                save_path=output_path / 'error_distribution.png')
        
        logger.info(f"✓ Plots saved to {output_path}")
    
    # Combine results
    results = {
        'metrics': metrics,
        'residuals': residuals,
        'predictions': {
            'y_true': y_test.tolist(),
            'y_pred': y_pred.tolist()
        }
    }
    
    logger.info("\n✓ Evaluation complete")
    logger.info("=" * 60)
    
    return results


def calculate_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """
    Calculate comprehensive evaluation metrics
    
    Args:
        y_true: True values
        y_pred: Predicted values
    
    Returns:
        Dict with various metrics
    """
    
    metrics = {
        'rmse': float(np.sqrt(mean_squared_error(y_true, y_pred))),
        'mae': float(mean_absolute_error(y_true, y_pred)),
        'r2': float(r2_score(y_true, y_pred)),
        'mape': float(mean_absolute_percentage_error(y_true, y_pred) * 100),
        'mse': float(mean_squared_error(y_true, y_pred))
    }
    
    # Bias
    metrics['bias'] = float(np.mean(y_pred - y_true))
    
    # Explained variance
    metrics['explained_variance'] = float(1 - np.var(y_true - y_pred) / np.var(y_true))
    
    return metrics


def calculate_residuals(y_true: np.ndarray, y_pred: np.ndarray) -> Dict:
    """
    Calculate residual statistics
    
    Args:
        y_true: True values
        y_pred: Predicted values
    
    Returns:
        Dict with residual statistics
    """
    
    residuals_array = y_pred - y_true
    
    return {
        'mean': float(np.mean(residuals_array)),
        'std': float(np.std(residuals_array)),
        'min': float(np.min(residuals_array)),
        'max': float(np.max(residuals_array)),
        'max_positive': float(np.max(residuals_array)),  # Overestimation
        'max_negative': float(np.min(residuals_array)),  # Underestimation
        'q25': float(np.percentile(residuals_array, 25)),
        'q50': float(np.percentile(residuals_array, 50)),
        'q75': float(np.percentile(residuals_array, 75))
    }


def plot_actual_vs_predicted(y_true: np.ndarray, y_pred: np.ndarray, 
                             metrics: Dict, save_path: Optional[Path] = None):
    """
    Create scatter plot of actual vs predicted values
    
    Args:
        y_true: True values
        y_pred: Predicted values
        metrics: Evaluation metrics dict
        save_path: Path to save plot (optional)
    """
    
    plt.figure(figsize=(10, 8))
    
    # Scatter plot
    plt.scatter(y_true, y_pred, alpha=0.5, edgecolors='k', linewidth=0.5)
    
    # Perfect prediction line
    min_val = min(y_true.min(), y_pred.min())
    max_val = max(y_true.max(), y_pred.max())
    plt.plot([min_val, max_val], [min_val, max_val], 'r--', lw=2, label='Perfect Prediction')
    
    # Add metrics text
    text_str = f"R² = {metrics['r2']:.4f}\n"
    text_str += f"RMSE = {metrics['rmse']:.2f} Mg/ha\n"
    text_str += f"MAE = {metrics['mae']:.2f} Mg/ha\n"
    text_str += f"Bias = {metrics['bias']:.2f} Mg/ha"
    
    plt.text(0.05, 0.95, text_str, transform=plt.gca().transAxes,
             fontsize=10, verticalalignment='top',
             bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
    
    plt.xlabel('Actual Carbon (Mg/ha)', fontsize=12)
    plt.ylabel('Predicted Carbon (Mg/ha)', fontsize=12)
    plt.title('Actual vs Predicted Carbon Stock', fontsize=14, fontweight='bold')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        logger.info(f"✓ Saved: {save_path.name}")
    else:
        plt.show()
    
    plt.close()


def plot_residuals(y_true: np.ndarray, y_pred: np.ndarray, 
                   residuals: Dict, save_path: Optional[Path] = None):
    """
    Create residual plot
    
    Args:
        y_true: True values
        y_pred: Predicted values
        residuals: Residual statistics dict
        save_path: Path to save plot (optional)
    """
    
    residuals_array = y_pred - y_true
    
    plt.figure(figsize=(10, 6))
    
    # Residual scatter
    plt.scatter(y_pred, residuals_array, alpha=0.5, edgecolors='k', linewidth=0.5)
    
    # Zero line
    plt.axhline(y=0, color='r', linestyle='--', lw=2, label='Zero Residual')
    
    # Add mean and std lines
    plt.axhline(y=residuals['mean'], color='blue', linestyle='-', lw=1, 
                label=f'Mean = {residuals["mean"]:.2f}')
    plt.axhline(y=residuals['mean'] + 2*residuals['std'], color='green', 
                linestyle=':', lw=1, label=f'±2 Std Dev')
    plt.axhline(y=residuals['mean'] - 2*residuals['std'], color='green', 
                linestyle=':', lw=1)
    
    plt.xlabel('Predicted Carbon (Mg/ha)', fontsize=12)
    plt.ylabel('Residual (Mg/ha)', fontsize=12)
    plt.title('Residual Plot', fontsize=14, fontweight='bold')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        logger.info(f"✓ Saved: {save_path.name}")
    else:
        plt.show()
    
    plt.close()


def plot_error_distribution(y_true: np.ndarray, y_pred: np.ndarray,
                            save_path: Optional[Path] = None):
    """
    Create error distribution histogram
    
    Args:
        y_true: True values
        y_pred: Predicted values
        save_path: Path to save plot (optional)
    """
    
    errors = y_pred - y_true
    
    plt.figure(figsize=(10, 6))
    
    # Histogram
    plt.hist(errors, bins=50, edgecolor='black', alpha=0.7, color='skyblue')
    
    # Add vertical lines
    plt.axvline(x=0, color='red', linestyle='--', lw=2, label='Zero Error')
    plt.axvline(x=np.mean(errors), color='green', linestyle='-', lw=2, 
                label=f'Mean = {np.mean(errors):.2f}')
    
    plt.xlabel('Prediction Error (Mg/ha)', fontsize=12)
    plt.ylabel('Frequency', fontsize=12)
    plt.title('Error Distribution', fontsize=14, fontweight='bold')
    plt.legend()
    plt.grid(True, alpha=0.3, axis='y')
    
    if save_path:
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        logger.info(f"✓ Saved: {save_path.name}")
    else:
        plt.show()
    
    plt.close()


def cross_validate_model(model, X: np.ndarray, y: np.ndarray, 
                        cv: int = 5) -> Dict:
    """
    Perform cross-validation on model
    
    Args:
        model: sklearn-compatible model
        X: Feature array
        y: Target array
        cv: Number of cross-validation folds
    
    Returns:
        Dict with cross-validation results
    """
    
    logger.info(f"Performing {cv}-fold cross-validation...")
    
    # Scoring metrics
    scoring = ['neg_mean_squared_error', 'neg_mean_absolute_error', 'r2']
    
    results = {}
    
    for metric in scoring:
        scores = cross_val_score(model, X, y, cv=cv, scoring=metric, n_jobs=-1)
        
        metric_name = metric.replace('neg_', '').replace('_', ' ').title()
        
        if 'neg' in metric:
            scores = -scores  # Convert back to positive
        
        results[metric_name] = {
            'mean': float(np.mean(scores)),
            'std': float(np.std(scores)),
            'min': float(np.min(scores)),
            'max': float(np.max(scores))
        }
        
        logger.info(f"{metric_name}: {np.mean(scores):.4f} ± {np.std(scores):.4f}")
    
    # Get cross-validated predictions
    y_pred_cv = cross_val_predict(model, X, y, cv=cv, n_jobs=-1)
    
    # Calculate RMSE from MSE
    if 'Mean Squared Error' in results:
        results['RMSE'] = {
            'mean': float(np.sqrt(results['Mean Squared Error']['mean'])),
            'std': float(np.sqrt(results['Mean Squared Error']['std']))
        }
    
    results['predictions'] = {
        'y_true': y.tolist(),
        'y_pred': y_pred_cv.tolist()
    }
    
    logger.info("✓ Cross-validation complete")
    
    return results


def compare_models(models_dict: Dict, X_test: np.ndarray, y_test: np.ndarray) -> Dict:
    """
    Compare multiple models on the same test set
    
    Args:
        models_dict: Dict mapping model names to model instances
        X_test: Test features
        y_test: Test targets
    
    Returns:
        Dict with comparison results
    """
    
    logger.info("=" * 60)
    logger.info("MODEL COMPARISON")
    logger.info("=" * 60)
    
    results = {}
    
    for model_name, model in models_dict.items():
        logger.info(f"\nEvaluating: {model_name}")
        
        y_pred = model.predict(X_test)
        metrics = calculate_metrics(y_test, y_pred)
        
        results[model_name] = metrics
        
        logger.info(f"  RMSE: {metrics['rmse']:.2f}")
        logger.info(f"  R²: {metrics['r2']:.4f}")
    
    # Find best model
    best_model = min(results.items(), key=lambda x: x[1]['rmse'])
    
    logger.info("\n" + "=" * 60)
    logger.info(f"🏆 BEST MODEL: {best_model[0]}")
    logger.info(f"   RMSE: {best_model[1]['rmse']:.2f} Mg/ha")
    logger.info(f"   R²: {best_model[1]['r2']:.4f}")
    logger.info("=" * 60)
    
    return {
        'comparison': results,
        'best_model': {
            'name': best_model[0],
            'metrics': best_model[1]
        }
    }


def generate_evaluation_report(results: Dict, output_file: str = 'evaluation_report.txt'):
    """
    Generate text report of evaluation results
    
    Args:
        results: Evaluation results dict
        output_file: Path to save report
    """
    
    with open(output_file, 'w') as f:
        f.write("=" * 70 + "\n")
        f.write("CARBON ESTIMATION MODEL - EVALUATION REPORT\n")
        f.write("=" * 70 + "\n\n")
        
        # Metrics
        f.write("TEST SET PERFORMANCE\n")
        f.write("-" * 70 + "\n")
        metrics = results['metrics']
        f.write(f"Root Mean Squared Error (RMSE): {metrics['rmse']:.2f} Mg/ha\n")
        f.write(f"Mean Absolute Error (MAE):      {metrics['mae']:.2f} Mg/ha\n")
        f.write(f"R² Score:                        {metrics['r2']:.4f}\n")
        f.write(f"Mean Absolute Percentage Error:  {metrics['mape']:.2f}%\n")
        f.write(f"Bias:                            {metrics['bias']:.2f} Mg/ha\n")
        f.write(f"Explained Variance:              {metrics['explained_variance']:.4f}\n\n")
        
        # Residuals
        f.write("RESIDUAL ANALYSIS\n")
        f.write("-" * 70 + "\n")
        residuals = results['residuals']
        f.write(f"Mean Residual:          {residuals['mean']:.2f} Mg/ha\n")
        f.write(f"Std Dev of Residuals:   {residuals['std']:.2f} Mg/ha\n")
        f.write(f"Max Overestimation:     {residuals['max_positive']:.2f} Mg/ha\n")
        f.write(f"Max Underestimation:    {residuals['max_negative']:.2f} Mg/ha\n")
        f.write(f"25th Percentile:        {residuals['q25']:.2f} Mg/ha\n")
        f.write(f"Median (50th):          {residuals['q50']:.2f} Mg/ha\n")
        f.write(f"75th Percentile:        {residuals['q75']:.2f} Mg/ha\n\n")
        
        f.write("=" * 70 + "\n")
        f.write("Report generated at: " + str(Path(output_file).absolute()) + "\n")
        f.write("=" * 70 + "\n")
    
    logger.info(f"✓ Evaluation report saved: {output_file}")


if __name__ == '__main__':
    # Example usage
    print("Model Evaluation Module")
    print("Import this module in your training script:")
    print("  from training.model_evaluation import evaluate_model, compare_models")