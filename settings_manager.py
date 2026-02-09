# settings_manager.py - Manages saving and loading of adjustment presets

import json
import sys
from pathlib import Path


def get_settings_path():
    """Get the appropriate settings path based on environment"""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        settings_dir = Path.home() / ".libs_settings"
    else:
        # Running from source
        settings_dir = Path(__file__).parent / ".libs_settings"
    
    settings_dir.mkdir(exist_ok=True)
    return settings_dir / "settings.json"


def save_settings(settings_dict):
    """Save all adjustment settings to JSON file"""
    try:
        settings_path = get_settings_path()
        with open(settings_path, 'w') as f:
            json.dump(settings_dict, f, indent=2)
        return True, "Settings saved successfully"
    except Exception as e:
        return False, f"Error saving settings: {str(e)}"


def load_settings():
    """Load settings from JSON file, return None if file doesn't exist"""
    try:
        settings_path = get_settings_path()
        if settings_path.exists():
            with open(settings_path, 'r') as f:
                return json.load(f)
        return None
    except Exception as e:
        return None


def delete_settings():
    """Delete the settings file"""
    try:
        settings_path = get_settings_path()
        if settings_path.exists():
            settings_path.unlink()
        return True, "Settings deleted successfully"
    except Exception as e:
        return False, f"Error deleting settings: {str(e)}"


def get_default_settings():
    """Return the default settings"""
    return {
        "adjust_spectrum": {
            "smoothing_method": "Moving average",
            "smoothing_strength": 1,
            "laser_removal_enabled": False,
            "laser_wavelength": 532.63,
            "laser_removal_width": 2.0,
            "baseline_removal_enabled": False,
            "baseline_smoothness": "Medium (1e4)",
            "baseline_asymmetry": "Balanced (0.001)"
        },
        "adjust_plot": {
            "x_axis_start": 100.0,
            "x_axis_end": 1000.0,
            "y_axis_start": 0.0,
            "y_axis_end": 1.0,
            "normalize_method": "None",
            "line_color": "#000000",
            "background_color": "#FFFFFF",
            "line_width": 1.0
        }
    }


def capture_spectrum_settings(smooth_method_var, smooth_strength_slider, 
                             laser_removal_var, laser_wavelength_var, 
                             laser_width_var, baseline_removal_var,
                             smoothness_preset_var, asymmetry_preset_var):
    """Capture current spectrum adjustment settings from GUI variables"""
    settings = {
        "smoothing_method": smooth_method_var.get(),
        "smoothing_strength": int(float(smooth_strength_slider.get())),
        "laser_removal_enabled": laser_removal_var.get(),
        "laser_wavelength": laser_wavelength_var.get(),
        "laser_removal_width": laser_width_var.get(),
        "baseline_removal_enabled": baseline_removal_var.get(),
        "baseline_smoothness": smoothness_preset_var.get(),
        "baseline_asymmetry": asymmetry_preset_var.get()
    }
    return settings


def apply_spectrum_settings(settings, smooth_method_var, smooth_strength_slider,
                           laser_removal_var, laser_wavelength_var,
                           laser_width_var, baseline_removal_var,
                           smoothness_preset_var, asymmetry_preset_var):
    """Apply saved spectrum settings to GUI variables"""
    if not settings or "adjust_spectrum" not in settings:
        return False
    
    try:
        s = settings["adjust_spectrum"]
        smooth_method_var.set(s.get("smoothing_method", "Moving average"))
        smooth_strength_slider.set(s.get("smoothing_strength", 1))
        laser_removal_var.set(s.get("laser_removal_enabled", False))
        laser_wavelength_var.set(s.get("laser_wavelength", 532.63))
        laser_width_var.set(s.get("laser_removal_width", 2.0))
        baseline_removal_var.set(s.get("baseline_removal_enabled", False))
        smoothness_preset_var.set(s.get("baseline_smoothness", "Medium (1e6)"))
        asymmetry_preset_var.set(s.get("baseline_asymmetry", "Balanced (0.001)"))
        return True
    except Exception as e:
        print(f"Error applying settings: {str(e)}")
        return False


def capture_plot_settings(x_start_var, x_end_var, y_start_var, y_end_var,
                         line_color_var, bg_color_var, line_width_var, normalize_var):
    """Capture current plot adjustment settings from GUI variables"""
    settings = {
        "x_axis_start": x_start_var.get(),
        "x_axis_end": x_end_var.get(),
        "y_axis_start": y_start_var.get(),
        "y_axis_end": y_end_var.get(),
        "line_color": line_color_var.get(),
        "background_color": bg_color_var.get(),
        "line_width": line_width_var.get(),
        "normalize_method": normalize_var.get()
    }
    return settings


def apply_plot_settings(settings, x_start_var, x_end_var, y_start_var, y_end_var,
                       line_color_var, bg_color_var, line_width_var, normalize_var):
    """Apply saved plot settings to GUI variables"""
    if not settings or "adjust_plot" not in settings:
        return False
    
    try:
        p = settings["adjust_plot"]
        x_start_var.set(p.get("x_axis_start", 100.0))
        x_end_var.set(p.get("x_axis_end", 1000.0))
        y_start_var.set(p.get("y_axis_start", 0.0))
        y_end_var.set(p.get("y_axis_end", 1.0))
        line_color_var.set(p.get("line_color", "#000000"))
        bg_color_var.set(p.get("background_color", "#FFFFFF"))
        line_width_var.set(p.get("line_width", 1.0))
        # Backward compat: old settings stored normalize_enabled as bool
        method = p.get("normalize_method", None)
        if method is None:
            method = "Min-Max" if p.get("normalize_enabled", False) else "None"
        normalize_var.set(method)
        return True
    except Exception as e:
        print(f"Error applying plot settings: {str(e)}")
        return False
