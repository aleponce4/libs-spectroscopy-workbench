# spectrometer.py - Hardware abstraction layer for optical spectrometers.
#
# Multi-brand architecture:
#   SpectrometerBase      – ABC defining the interface every backend must implement.
#   SpectrometerModule    – Ocean Optics / Ocean Insight backend  (python-seabreeze).
#   ThorlabsCCSModule     – Thorlabs CCS-series backend  (TLCCS_64.dll via ctypes).
#
# All backends are lazy-loaded: the third-party library is imported only inside
# connect(), so the analysis side of the app never needs any spectrometer driver.

import numpy as np
import time
import logging
import subprocess
import json
import re
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  USB bus scanner (Windows – PowerShell based, zero external deps)
# ═══════════════════════════════════════════════════════════════════════

# Known USB vendor IDs for supported spectrometer brands
_OCEAN_OPTICS_VID = "2457"   # 0x2457
_THORLABS_VID     = "1313"   # 0x1313


def scan_usb_spectrometers() -> list[dict]:
    """
    Query the Windows USB bus for spectrometer-class devices.

    Returns a list of dicts, each containing::

        {
            "vid": "2457",
            "pid": "1022",
            "description": "USB4000 Ocean Optics",
            "driver": "WinUSB" | "libusb-win32" | "libusbK" | …,
            "status": "OK" | "Error" | …,
            "instance_id": "USB\\VID_2457&PID_1022\\...",
            "brand": "ocean_optics" | "thorlabs" | "unknown",
        }

    Falls back gracefully to an empty list if PowerShell is unavailable.
    """
    target_vids = {_OCEAN_OPTICS_VID, _THORLABS_VID}

    # PowerShell command: list PnP devices whose InstanceId contains a target VID
    # We fetch all USB devices and filter in Python for simplicity.
    ps_script = (
        "Get-PnpDevice -Class USB,HIDClass,Ports,USBDevice -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -match 'USB\\\\VID_' } | "
        "Select-Object InstanceId, FriendlyName, Status, "
        "@{N='Driver';E={(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_DriverDesc' -ErrorAction SilentlyContinue).Data}} | "
        "ConvertTo-Json -Compress"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug(f"USB scan returned no data (rc={result.returncode})")
            return []

        raw = result.stdout.strip()
        # PowerShell returns a single object (not array) when only one match
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        logger.debug(f"USB scan failed: {e}")
        return []

    devices = []
    for entry in data:
        iid = entry.get("InstanceId", "")
        vid_match = re.search(r"VID_(\w{4})", iid, re.IGNORECASE)
        pid_match = re.search(r"PID_(\w{4})", iid, re.IGNORECASE)
        if not vid_match:
            continue
        vid = vid_match.group(1).upper()
        pid = pid_match.group(1).upper() if pid_match else "????"

        if vid not in {v.upper() for v in target_vids}:
            continue

        brand = "unknown"
        if vid == _OCEAN_OPTICS_VID.upper():
            brand = "ocean_optics"
        elif vid == _THORLABS_VID.upper():
            brand = "thorlabs"

        devices.append({
            "vid": vid,
            "pid": pid,
            "description": entry.get("FriendlyName") or "Unknown device",
            "driver": entry.get("Driver") or "(no driver)",
            "status": entry.get("Status") or "Unknown",
            "instance_id": iid,
            "brand": brand,
        })

    return devices


def _driver_ok_for_backend(driver_name: str, backend: str) -> tuple[bool, str]:
    """
    Check whether a USB driver name is compatible with the given seabreeze
    backend ('cseabreeze' or 'pyseabreeze').

    Returns (is_ok, advice_string).
    """
    d = (driver_name or "").lower()
    if backend == "cseabreeze":
        if "winusb" in d:
            return True, ""
        if "libusb" in d or "libusbk" in d:
            return False, (
                "cseabreeze needs WinUSB driver.\n"
                "Run:  seabreeze_os_setup  (from an admin terminal) "
                "to switch the driver."
            )
        return False, f"Unknown driver '{driver_name}' — may need WinUSB for cseabreeze."
    elif backend == "pyseabreeze":
        if "libusb" in d or "libusbk" in d:
            return True, ""
        if "winusb" in d:
            return False, (
                "pyseabreeze needs libusb/libusbK driver.\n"
                "Install libusb-win32 via Zadig (https://zadig.akeo.ie) "
                "or switch to cseabreeze backend."
            )
        return False, f"Unknown driver '{driver_name}' — may need libusb for pyseabreeze."
    return True, ""


# ═══════════════════════════════════════════════════════════════════════
#  Exceptions
# ═══════════════════════════════════════════════════════════════════════

class SpectrometerError(Exception):
    """Custom exception for spectrometer-related errors."""
    pass


class NoDeviceError(SpectrometerError):
    """Raised specifically when no spectrometer hardware is found.
    The GUI can catch this to offer simulation mode."""
    pass


# ═══════════════════════════════════════════════════════════════════════
#  Device Capabilities – a simple data class returned after connection
# ═══════════════════════════════════════════════════════════════════════

class DeviceCapabilities:
    """
    Read-only snapshot of the connected spectrometer's capabilities.
    
    Populated at connect-time; used by the GUI to configure itself
    dynamically (axis limits, trigger buttons, correction check-boxes …).
    """

    def __init__(self):
        # ── Identity ───────────────────────────────────────────────
        self.model: str = "N/A"
        self.serial_number: str = "N/A"
        self.brand: str = "unknown"          # "ocean_optics", "simulated", …

        # ── Sensor geometry ────────────────────────────────────────
        self.pixel_count: int = 0
        self.wavelength_min: float = 0.0     # nm
        self.wavelength_max: float = 0.0     # nm

        # ── ADC / dynamic range ────────────────────────────────────
        self.max_intensity: float = 65535.0  # counts at saturation

        # ── Integration time ───────────────────────────────────────
        self.integration_time_min_us: int = 10
        self.integration_time_max_us: int = 65_535_000

        # ── Trigger modes ──────────────────────────────────────────
        #   Keys are semantic names, values are the integer codes
        #   accepted by the hardware.  At minimum there should be
        #   "normal"; "external" is present when the device supports
        #   an external hardware trigger.
        self.trigger_modes: dict[str, int] = {"normal": 0}

        # ── Feature support flags ──────────────────────────────────
        self.supports_dark_correction: bool = False
        self.supports_nonlinearity_correction: bool = False

    # Convenience helpers for the most common queries
    @property
    def normal_trigger_mode(self) -> int:
        """Integer code to put the spectrometer into free-running mode."""
        return self.trigger_modes.get("normal", 0)

    @property
    def external_trigger_mode(self) -> int | None:
        """Integer code for external hardware trigger, or None if unsupported."""
        return self.trigger_modes.get("external")

    @property
    def has_external_trigger(self) -> bool:
        return "external" in self.trigger_modes


# ═══════════════════════════════════════════════════════════════════════
#  SpectrometerBase — Abstract Base Class for all backends
# ═══════════════════════════════════════════════════════════════════════

class SpectrometerBase(ABC):
    """
    Abstract base class that every spectrometer backend must implement.
    
    The GUI (acquisition_app / acquisition_worker) depends **only** on this
    interface, so adding a new brand is just a matter of subclassing and
    implementing the abstract methods below.
    """

    # ─── Properties every backend must expose ──────────────────────────

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def capabilities(self) -> DeviceCapabilities: ...

    @property
    @abstractmethod
    def integration_time_us(self) -> int: ...

    @property
    @abstractmethod
    def current_trigger_mode(self) -> int: ...

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def serial_number(self) -> str: ...

    # ─── Connection lifecycle ──────────────────────────────────────────

    @abstractmethod
    def connect(self, device_index: int = 0) -> str:
        """Open the spectrometer.  Returns a status string."""
        ...

    @abstractmethod
    def connect_simulated(self, profile_name: str = "Generic") -> str:
        """Open a simulated spectrometer.  Returns a status string."""
        ...

    @abstractmethod
    def disconnect(self) -> None: ...

    # ─── Configuration ─────────────────────────────────────────────────

    @abstractmethod
    def set_integration_time(self, microseconds: int) -> None: ...

    @abstractmethod
    def set_trigger_mode(self, mode: int) -> None: ...

    # ─── Data acquisition ──────────────────────────────────────────────

    @abstractmethod
    def get_wavelengths(self) -> np.ndarray: ...

    @abstractmethod
    def get_intensities(self, correct_dark_counts: bool = False,
                         correct_nonlinearity: bool = False) -> np.ndarray: ...

    def get_spectrum(self) -> tuple:
        """Convenience — returns (wavelengths, intensities)."""
        return self.get_wavelengths(), self.get_intensities()

    # ─── Optional helpers ──────────────────────────────────────────────

    def list_available_devices(self) -> list:
        """Return [(model, serial, device_obj), …].  Override if the backend
        supports device discovery."""
        return []

    @classmethod
    def diagnose(cls) -> dict:
        """Run diagnostics and return a structured report.
        Override in subclasses.  Default returns empty report."""
        return {"backend": cls.__name__, "notes": [], "devices": []}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __del__(self):
        try:
            self.disconnect()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  Simulated Spectrometer (for testing without hardware)
# ═══════════════════════════════════════════════════════════════════════

# Default simulation profiles – easy to extend
SIMULATION_PROFILES = {
    "USB4000": {
        "model": "USB4000-SIM",
        "pixels": 3648,
        "wl_min": 200.0,
        "wl_max": 1100.0,
        "max_intensity": 65535,
        "int_min_us": 10,
        "int_max_us": 65_535_000,
        "trigger_modes": {"normal": 0, "external": 3},
    },
    "QEPro": {
        "model": "QEPRO-SIM",
        "pixels": 1044,
        "wl_min": 200.0,
        "wl_max": 950.0,
        "max_intensity": 262143,
        "int_min_us": 8_000,
        "int_max_us": 1_600_000_000,
        "trigger_modes": {"normal": 0, "external": 3},
    },
    "HDX": {
        "model": "HDX-SIM",
        "pixels": 2068,
        "wl_min": 200.0,
        "wl_max": 800.0,
        "max_intensity": 65535,
        "int_min_us": 6_000,
        "int_max_us": 10_000_000,
        "trigger_modes": {"normal": 0, "external": 1},
    },
    "Generic": {
        "model": "GENERIC-SIM",
        "pixels": 2048,
        "wl_min": 200.0,
        "wl_max": 1000.0,
        "max_intensity": 65535,
        "int_min_us": 1_000,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0, "external": 3},
    },
    # ── Thorlabs CCS profiles ────────────────────────────────────
    "CCS175": {
        "model": "CCS175-SIM",
        "pixels": 3648,
        "wl_min": 500.0,
        "wl_max": 1000.0,
        "max_intensity": 1.0,      # Thorlabs CCS returns normalised 0.0–1.0
        "int_min_us": 10,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0},  # CCS has no external HW trigger
    },
    "CCS200": {
        "model": "CCS200-SIM",
        "pixels": 3648,
        "wl_min": 200.0,
        "wl_max": 1000.0,
        "max_intensity": 1.0,
        "int_min_us": 10,
        "int_max_us": 60_000_000,
        "trigger_modes": {"normal": 0},
    },
}


class _SimulatedSpectrometer:
    """
    Fake spectrometer that generates realistic-looking LIBS spectra.
    
    Accepts a *profile* dict (see SIMULATION_PROFILES) so the simulator
    can mimic different hardware characteristics.
    """

    def __init__(self, profile: dict | None = None):
        if profile is None:
            profile = SIMULATION_PROFILES["Generic"]

        self.is_open = True
        self.model = profile.get("model", "GENERIC-SIM")
        self.serial_number = "SIM00001"
        self._integration_time_us = 100_000
        self._trigger_mode = 0

        self._pixels = profile.get("pixels", 2048)
        self._max_intensity = profile.get("max_intensity", 65535)
        self._wl_min = profile.get("wl_min", 200.0)
        self._wl_max = profile.get("wl_max", 1000.0)
        self._int_min_us = profile.get("int_min_us", 1_000)
        self._int_max_us = profile.get("int_max_us", 60_000_000)
        self._trigger_modes = profile.get("trigger_modes", {"normal": 0, "external": 3})

        self._wavelengths = np.linspace(self._wl_min, self._wl_max, self._pixels)

        # Synthetic LIBS emission lines — (center_nm, relative_intensity, width_nm)
        self._emission_lines = [
            # Iron (Fe)
            (238.20, 0.45, 0.15), (239.56, 0.50, 0.15), (240.49, 0.35, 0.15),
            (248.33, 0.40, 0.15), (252.28, 0.38, 0.15), (259.94, 0.55, 0.15),
            (271.44, 0.30, 0.15), (273.95, 0.42, 0.15), (275.57, 0.35, 0.15),
            (358.12, 0.60, 0.18), (371.99, 0.75, 0.18), (373.49, 0.65, 0.18),
            (374.56, 0.50, 0.18), (382.04, 0.55, 0.18), (385.99, 0.80, 0.18),
            (404.58, 0.45, 0.18),
            # Calcium (Ca)
            (393.37, 0.90, 0.20), (396.85, 0.85, 0.20),
            (422.67, 0.70, 0.18), (445.48, 0.30, 0.15),
            # Sodium (Na)
            (588.99, 0.95, 0.22), (589.59, 0.90, 0.22),
            # Hydrogen (H-alpha)
            (656.28, 0.55, 0.25),
            # Magnesium (Mg)
            (279.55, 0.60, 0.15), (280.27, 0.55, 0.15), (285.21, 0.65, 0.15),
            # Silicon (Si)
            (251.61, 0.40, 0.15), (288.16, 0.50, 0.15),
            # Aluminum (Al)
            (308.22, 0.45, 0.18), (309.27, 0.40, 0.18), (394.40, 0.55, 0.18),
            (396.15, 0.50, 0.18),
            # Oxygen (O)
            (777.19, 0.70, 0.25), (777.42, 0.65, 0.25), (844.64, 0.45, 0.22),
            # Nitrogen (N)
            (742.36, 0.30, 0.20), (744.23, 0.35, 0.20), (746.83, 0.25, 0.20),
        ]

    def wavelengths(self):
        return self._wavelengths.copy()

    def intensities(self, **_kwargs):
        # Simulate trigger delay when in an external-trigger-like mode
        ext_mode = self._trigger_modes.get("external")
        if ext_mode is not None and self._trigger_mode == ext_mode:
            delay = np.random.uniform(1.0, 3.0)
            time.sleep(delay)

        # Build spectrum: baseline + peaks + noise
        baseline = 250 + 50 * np.sin(self._wavelengths / 200.0)
        scale = self._integration_time_us / 100_000.0
        spectrum = baseline.copy()

        for center, rel_intensity, width in self._emission_lines:
            intensity = rel_intensity * 3500 * scale
            intensity *= np.random.uniform(0.85, 1.15)
            width_jitter = width * np.random.uniform(0.9, 1.1)
            spectrum += intensity * np.exp(
                -0.5 * ((self._wavelengths - center) / width_jitter) ** 2
            )

        shot_noise = np.sqrt(np.maximum(spectrum, 0)) * np.random.randn(self._pixels) * 0.5
        readout_noise = np.random.randn(self._pixels) * 8
        spectrum += shot_noise + readout_noise

        # Normalise to the device's dynamic range.
        # The raw generation assumes a ~65 535 ADC scale; for devices with a
        # different max_intensity (e.g. Thorlabs CCS uses 0.0–1.0) we
        # rescale so the simulated data looks correct on the graph.
        spectrum = np.clip(spectrum, 0, 65535)
        if self._max_intensity != 65535:
            spectrum = spectrum / 65535.0 * float(self._max_intensity)
        return spectrum

    def integration_time_micros(self, us):
        self._integration_time_us = us

    def trigger_mode(self, mode):
        self._trigger_mode = mode

    def close(self):
        self.is_open = False


# ═══════════════════════════════════════════════════════════════════════
#  Trigger mode mapping helpers
# ═══════════════════════════════════════════════════════════════════════

# Known trigger-mode name → int mappings for seabreeze OOI and OBP protocols.
_NORMAL_MODE_NAMES = {"NORMAL", "OBP_NORMAL"}
_EXTERNAL_TRIGGER_MODE_NAMES = {
    "HARDWARE", "EDGE", "OBP_EXTERNAL", "OBP_EDGE",
    "SYNCHRONIZATION", "LEVEL", "EXTERNAL",
}

# ── Model-based fallback table ─────────────────────────────────────
# The cseabreeze C-wrapper (v2.4+) does NOT expose _trigger_modes or
# _feature_classes, so runtime introspection fails.  This table provides
# known trigger modes for common Ocean Optics models so the "Arm Trigger"
# button works even when introspection is unavailable.
#
# Source: Ocean Optics OOI protocol documentation & USB4000 datasheet.
#   0 = Normal (free-running)
#   1 = Software trigger
#   2 = External level (synchronisation) trigger
#   3 = External edge trigger          ← the one used for LIBS
#
# Models that share the OOI protocol and the same mode numbers:
_OOI_EDGE_TRIGGER_MODELS = {
    "USB2000", "USB2000+", "USB4000", "HR2000", "HR2000+",
    "HR4000", "QE65000", "QE65Pro", "QEPro", "FLAME-S",
    "FLAME-T", "Maya2000", "Maya2000Pro", "NIRQuest256",
    "NIRQuest512",
}

_MODEL_TRIGGER_FALLBACKS: dict[str, dict[str, int]] = {}
for _m in _OOI_EDGE_TRIGGER_MODELS:
    _MODEL_TRIGGER_FALLBACKS[_m] = {"normal": 0, "external": 3}

# OBP-protocol devices (HDX, Ocean ST / SR, etc.) use different codes:
_MODEL_TRIGGER_FALLBACKS["HDX"] = {"normal": 0, "external": 1}
_MODEL_TRIGGER_FALLBACKS["Ocean-ST"]  = {"normal": 0, "external": 1}
_MODEL_TRIGGER_FALLBACKS["Ocean-SR2"] = {"normal": 0, "external": 1}
_MODEL_TRIGGER_FALLBACKS["Ocean-SR4"] = {"normal": 0, "external": 1}
_MODEL_TRIGGER_FALLBACKS["Ocean-SR6"] = {"normal": 0, "external": 1}


def _build_trigger_map_from_seabreeze(spec) -> dict[str, int]:
    """
    Inspect a real seabreeze Spectrometer to build a semantic trigger-mode map.
    
    Returns a dict like {"normal": 0, "external": 3} (or without "external"
    if the device has no external trigger support).
    
    Strategy:
        1. Try introspecting the seabreeze device object (works with pyseabreeze).
        2. If introspection fails (cseabreeze C-wrapper), fall back to a
           model-based lookup table of known Ocean Optics trigger modes.
    """
    trigger_map: dict[str, int] = {}

    try:
        raw_modes = None

        # Approach 1: get from the spectrometer feature on the underlying device
        try:
            spec_feature = spec._dev.features.get("spectrometer", [None])[0]
            if spec_feature and hasattr(spec_feature, '_trigger_modes'):
                raw_modes = spec_feature._trigger_modes
        except Exception:
            pass

        # Approach 2: get from the device class definition
        if raw_modes is None:
            try:
                dev_cls = type(spec._dev)
                if hasattr(dev_cls, '_feature_classes'):
                    for feat_list in dev_cls._feature_classes.get("spectrometer", []):
                        if hasattr(feat_list, '_trigger_modes'):
                            raw_modes = feat_list._trigger_modes
                            break
            except Exception:
                pass

        if raw_modes:
            for mode_val in raw_modes:
                mode_name = mode_val.name if hasattr(mode_val, 'name') else str(mode_val)
                mode_int = int(mode_val)

                name_upper = mode_name.upper()
                if name_upper in _NORMAL_MODE_NAMES:
                    trigger_map["normal"] = mode_int
                elif name_upper in _EXTERNAL_TRIGGER_MODE_NAMES:
                    trigger_map["external"] = mode_int
                # Also keep original mode names for power-users
                trigger_map[mode_name.lower()] = mode_int

    except Exception as e:
        logger.debug(f"Could not inspect trigger modes from device: {e}")

    # ── Fallback: model-based lookup when introspection found nothing ──
    if "external" not in trigger_map:
        model = spec.model if hasattr(spec, 'model') else ""
        fallback = _MODEL_TRIGGER_FALLBACKS.get(model)
        if fallback is not None:
            logger.info(
                f"Introspection did not find trigger modes for {model}; "
                f"using model-based fallback: {fallback}"
            )
            trigger_map.update(fallback)
        else:
            logger.info(
                f"No trigger mode introspection data and no fallback for "
                f"model '{model}'; only 'normal' mode is available."
            )

    # Ensure "normal" is always present
    if "normal" not in trigger_map:
        trigger_map["normal"] = 0

    return trigger_map


# ═══════════════════════════════════════════════════════════════════════
#  SpectrometerModule — main public interface
# ═══════════════════════════════════════════════════════════════════════

class SpectrometerModule(SpectrometerBase):
    """
    Ocean Optics / Ocean Insight backend via python-seabreeze.
    
    Model-agnostic:  all hardware-specific parameters (pixel count, trigger modes,
    integration-time limits, max intensity …) are queried from the device at connect-
    time and exposed through the ``capabilities`` property.

    Supports any spectrometer that python-seabreeze recognises (USB2000, USB4000,
    HR4000, QEPro, HDX, Flame, Ocean ST, …).
    """

    def __init__(self):
        self._spec = None
        self._sb = None                    # seabreeze module reference
        self._wavelengths = None
        self._current_trigger_mode: int = 0
        self._integration_time_us: int = 100_000   # 100 ms default
        self._simulated: bool = False
        self._capabilities = DeviceCapabilities()

    # ─── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Check if a spectrometer is currently connected and open."""
        if self._spec is None:
            return False
        if hasattr(self._spec, 'is_open'):
            return self._spec.is_open
        try:
            return self._spec._dev.is_open
        except (AttributeError, Exception):
            return self._spec is not None

    @property
    def capabilities(self) -> DeviceCapabilities:
        """The capabilities of the currently connected device (or defaults)."""
        return self._capabilities

    @property
    def integration_time_us(self) -> int:
        return self._integration_time_us

    @property
    def current_trigger_mode(self) -> int:
        return self._current_trigger_mode

    @property
    def model(self) -> str:
        """Return the spectrometer model string, or 'N/A' if not connected."""
        if self.is_connected:
            return self._spec.model
        return "N/A"

    @property
    def serial_number(self) -> str:
        """Return the spectrometer serial number, or 'N/A' if not connected."""
        if self.is_connected:
            return self._spec.serial_number
        return "N/A"

    # ─── Connection ────────────────────────────────────────────────────

    def list_available_devices(self) -> list:
        """
        Scan for connected spectrometers and return a list of
        (model, serial_number, device_object) tuples.
        
        Raises SpectrometerError if seabreeze is not installed.
        """
        self._lazy_import_seabreeze()
        from seabreeze.spectrometers import list_devices

        try:
            devices = list_devices()
        except Exception as e:
            raise SpectrometerError(f"Error scanning for spectrometers: {e}")

        result = []
        for dev in devices:
            try:
                m = dev.model
                s = dev.serial_number
            except Exception:
                m, s = "Unknown", "?"
            result.append((m, s, dev))
        return result

    def connect(self, device_index: int = 0) -> str:
        """
        Discover devices and open the spectrometer at *device_index* (default 0).
        
        All hardware parameters are queried and stored in ``self.capabilities``.
        
        Returns:
            A multi-line status string with device details.
        Raises:
            SpectrometerError / NoDeviceError on failure.
        """
        self._lazy_import_seabreeze()
        from seabreeze.spectrometers import Spectrometer, list_devices

        self._simulated = False

        # Discover devices
        try:
            devices = list_devices()
        except Exception as e:
            raise SpectrometerError(f"Error scanning for spectrometers: {e}")

        if not devices:
            raise NoDeviceError(
                "No spectrometer found.\n\n"
                "Check that:\n"
                "  1. The spectrometer is plugged in via USB\n"
                "  2. The USB driver is installed (run seabreeze_os_setup)\n"
                "  3. No other software (OceanView) is using the device"
            )

        if device_index >= len(devices):
            raise SpectrometerError(
                f"Device index {device_index} out of range — "
                f"only {len(devices)} device(s) found."
            )

        logger.info(f"Found {len(devices)} device(s):")
        for i, dev in enumerate(devices):
            logger.info(f"  [{i}] {dev}")

        # Open the chosen device
        try:
            self._spec = Spectrometer(devices[device_index])
        except Exception as e:
            raise SpectrometerError(f"Failed to open spectrometer: {e}")

        # ── Populate capabilities ──────────────────────────────────
        caps = DeviceCapabilities()
        caps.brand = "ocean_optics"
        caps.model = self.model
        caps.serial_number = self.serial_number

        self._wavelengths = self._spec.wavelengths()
        caps.pixel_count = self._spec.pixels
        caps.wavelength_min = round(float(self._wavelengths[0]), 1)
        caps.wavelength_max = round(float(self._wavelengths[-1]), 1)

        try:
            int_min_us, int_max_us = self._spec.integration_time_micros_limits
            caps.integration_time_min_us = int(int_min_us)
            caps.integration_time_max_us = int(int_max_us)
        except Exception:
            pass  # keep defaults

        try:
            caps.max_intensity = float(self._spec.max_intensity)
        except Exception:
            pass

        # Trigger modes — inspect the device
        caps.trigger_modes = _build_trigger_map_from_seabreeze(self._spec)
        if "external" not in caps.trigger_modes:
            logger.info(
                "Could not auto-detect external trigger mode; "
                "only 'normal' mode is available."
            )

        # Feature support flags
        try:
            features = self._spec.features
            caps.supports_nonlinearity_correction = bool(
                features.get("nonlinearity_coefficients")
            )
            caps.supports_dark_correction = True
        except Exception:
            pass

        self._capabilities = caps

        # ── Apply initial configuration ────────────────────────────
        self.set_integration_time(self._integration_time_us)
        self.set_trigger_mode(caps.normal_trigger_mode)

        # ── Verification read ──────────────────────────────────────
        try:
            test_spectrum = self._spec.intensities()
            if len(test_spectrum) != caps.pixel_count:
                logger.warning(
                    f"Verification: expected {caps.pixel_count} pixels, "
                    f"got {len(test_spectrum)}"
                )
            else:
                logger.info(f"Verification read OK: {caps.pixel_count} pixels returned")
        except Exception as e:
            try:
                self._spec.close()
            except Exception:
                pass
            self._spec = None
            self._wavelengths = None
            raise SpectrometerError(
                f"Spectrometer opened but failed verification read:\n{e}\n\n"
                "The device may be in a bad state. Try unplugging and reconnecting."
            )

        # ── Build status string ────────────────────────────────────
        status_lines = [f"Connected: {caps.model} (S/N: {caps.serial_number})"]
        status_lines.append(
            f"Pixels: {caps.pixel_count} | "
            f"Range: {caps.wavelength_min}–{caps.wavelength_max} nm"
        )
        int_min_ms = caps.integration_time_min_us / 1000.0
        int_max_ms = caps.integration_time_max_us / 1000.0
        status_lines.append(f"Integration: {int_min_ms}–{int_max_ms} ms")
        status_lines.append(f"Max intensity: {caps.max_intensity:.0f}")
        triggers = ", ".join(
            f"{name}={val}" for name, val in caps.trigger_modes.items()
        )
        status_lines.append(f"Trigger modes: {triggers}")

        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def connect_simulated(self, profile_name: str = "Generic") -> str:
        """
        Connect to the built-in simulated spectrometer.
        
        Args:
            profile_name: Key into SIMULATION_PROFILES (e.g. "USB4000", "QEPro",
                          "HDX", "Generic").
        
        Returns:
            A multi-line status string.
        """
        profile = SIMULATION_PROFILES.get(profile_name, SIMULATION_PROFILES["Generic"])
        self._spec = _SimulatedSpectrometer(profile)
        self._wavelengths = self._spec.wavelengths()
        self._integration_time_us = 100_000
        self._current_trigger_mode = 0
        self._simulated = True

        # Populate capabilities from the profile
        caps = DeviceCapabilities()
        caps.brand = "simulated"
        caps.model = self._spec.model
        caps.serial_number = self._spec.serial_number
        caps.pixel_count = self._spec._pixels
        caps.wavelength_min = round(self._spec._wl_min, 1)
        caps.wavelength_max = round(self._spec._wl_max, 1)
        caps.max_intensity = float(self._spec._max_intensity)
        caps.integration_time_min_us = self._spec._int_min_us
        caps.integration_time_max_us = self._spec._int_max_us
        caps.trigger_modes = dict(self._spec._trigger_modes)
        caps.supports_dark_correction = False
        caps.supports_nonlinearity_correction = False
        self._capabilities = caps

        wl_min = caps.wavelength_min
        wl_max = caps.wavelength_max

        status_lines = [
            f"Connected: {caps.model} (S/N: {caps.serial_number}) [SIMULATED]",
            f"Pixels: {caps.pixel_count} | Range: {wl_min}–{wl_max} nm",
            f"Integration: {caps.integration_time_min_us / 1000.0}–"
            f"{caps.integration_time_max_us / 1000.0} ms",
            f"Max intensity: {caps.max_intensity:.0f}",
        ]
        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def disconnect(self):
        """Safely close the spectrometer connection."""
        if self._spec is not None:
            try:
                normal = self._capabilities.normal_trigger_mode
                if self._current_trigger_mode != normal:
                    try:
                        self._spec.trigger_mode(normal)
                    except Exception:
                        pass
                self._spec.close()
                logger.info("Spectrometer disconnected.")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self._spec = None
                self._wavelengths = None
                self._current_trigger_mode = 0
                self._simulated = False
                self._capabilities = DeviceCapabilities()

    # ─── Configuration ─────────────────────────────────────────────────

    def set_integration_time(self, microseconds: int):
        """
        Set the integration time in microseconds.
        
        The value is clamped to the device's supported range
        (queried from capabilities).
        """
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        caps = self._capabilities
        microseconds = max(
            caps.integration_time_min_us,
            min(microseconds, caps.integration_time_max_us),
        )
        try:
            self._spec.integration_time_micros(microseconds)
            self._integration_time_us = microseconds
            logger.info(f"Integration time set to {microseconds} µs")
        except Exception as e:
            raise SpectrometerError(f"Failed to set integration time: {e}")

    def set_trigger_mode(self, mode: int):
        """
        Set the trigger mode by its integer code.
        
        Use ``capabilities.trigger_modes`` to discover which codes are valid
        for the connected device.
        """
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        valid_modes = set(self._capabilities.trigger_modes.values())
        if mode not in valid_modes:
            raise ValueError(
                f"Invalid trigger mode {mode} for {self.model}. "
                f"Supported: {self._capabilities.trigger_modes}"
            )

        try:
            self._spec.trigger_mode(mode)
            self._current_trigger_mode = mode
            logger.info(f"Trigger mode set to {mode}")
        except Exception as e:
            raise SpectrometerError(
                f"Failed to set trigger mode {mode}: {e}\n"
                "If the device appears frozen, try disconnecting and reconnecting."
            )

    # ─── Data Acquisition ──────────────────────────────────────────────

    def get_wavelengths(self) -> np.ndarray:
        """Return the wavelength array. Cached after first call."""
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")
        return self._wavelengths.copy()

    def get_intensities(self, correct_dark_counts: bool = False,
                         correct_nonlinearity: bool = False) -> np.ndarray:
        """
        Acquire one spectrum.
        
        In Normal mode, returns immediately after integration.
        In External Trigger mode, BLOCKS until a trigger signal is received.
        
        Args:
            correct_dark_counts: Subtract average dark pixel value (hardware only).
            correct_nonlinearity: Apply stored nonlinearity correction (hardware only).
        
        Returns:
            np.ndarray of intensity values.
        """
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        try:
            if self._simulated:
                return self._spec.intensities()
            return self._spec.intensities(
                correct_dark_counts=correct_dark_counts,
                correct_nonlinearity=correct_nonlinearity,
            )
        except Exception as e:
            raise SpectrometerError(f"Acquisition error: {e}")

    def get_spectrum(self) -> tuple:
        """Convenience method — returns (wavelengths, intensities)."""
        return self.get_wavelengths(), self.get_intensities()

    # ─── Internal helpers ──────────────────────────────────────────────

    @classmethod
    def diagnose(cls) -> dict:
        """
        Run a comprehensive diagnostic scan for Ocean Optics spectrometers.

        Returns a dict with::

            {
                "backend": "SpectrometerModule",
                "seabreeze_installed": bool,
                "seabreeze_backend": "cseabreeze" | "pyseabreeze" | None,
                "seabreeze_backend_fail_reason": str | None,
                "usb_devices": [...],          # from Windows USB bus scan
                "seabreeze_devices": [...],     # from list_devices()
                "per_device_errors": {...},      # serial → error string
                "driver_warnings": [...],       # per-device driver advice
                "notes": [str, ...],
            }
        """
        report: dict = {
            "backend": "SpectrometerModule",
            "seabreeze_installed": False,
            "seabreeze_backend": None,
            "seabreeze_backend_fail_reason": None,
            "usb_devices": [],
            "seabreeze_devices": [],
            "per_device_errors": {},
            "driver_warnings": [],
            "notes": [],
        }

        # 1. USB bus scan
        try:
            all_usb = scan_usb_spectrometers()
            report["usb_devices"] = [
                d for d in all_usb if d["brand"] == "ocean_optics"
            ]
        except Exception as e:
            report["notes"].append(f"USB bus scan error: {e}")

        # 2. Import seabreeze
        try:
            import seabreeze
            report["seabreeze_installed"] = True
        except ImportError:
            report["notes"].append(
                "python-seabreeze is NOT installed.\n"
                "Install with: pip install seabreeze"
            )
            return report

        # 3. Try cseabreeze, then pyseabreeze
        sb_backend = None
        c_fail = None
        py_fail = None
        try:
            seabreeze.use("cseabreeze")
            sb_backend = "cseabreeze"
        except Exception as e:
            c_fail = str(e)
            try:
                seabreeze.use("pyseabreeze")
                sb_backend = "pyseabreeze"
            except Exception as e2:
                py_fail = str(e2)

        report["seabreeze_backend"] = sb_backend
        if sb_backend is None:
            report["seabreeze_backend_fail_reason"] = (
                f"cseabreeze: {c_fail}\npyseabreeze: {py_fail}"
            )
            report["notes"].append("Neither seabreeze backend could be loaded.")
            return report
        elif c_fail:
            report["seabreeze_backend_fail_reason"] = (
                f"cseabreeze failed ({c_fail}), fell back to pyseabreeze"
            )

        # 4. Enumerate devices via seabreeze
        from seabreeze.spectrometers import list_devices, Spectrometer

        try:
            raw_devices = list_devices()
        except Exception as e:
            report["notes"].append(f"list_devices() error: {e}")
            raw_devices = []

        for i, dev in enumerate(raw_devices):
            try:
                m = dev.model
                s = dev.serial_number
            except Exception:
                m, s = "Unknown", f"device_{i}"
            report["seabreeze_devices"].append({
                "index": i, "model": m, "serial": s,
            })

            # 5. Try opening each device to find per-device errors
            try:
                test_spec = Spectrometer(dev)
                test_spec.close()
            except Exception as e:
                report["per_device_errors"][s] = str(e)

        # 6. Cross-reference USB bus vs seabreeze driver compatibility
        if sb_backend and report["usb_devices"]:
            for usb_dev in report["usb_devices"]:
                ok, advice = _driver_ok_for_backend(
                    usb_dev["driver"], sb_backend
                )
                if not ok:
                    report["driver_warnings"].append({
                        "device": usb_dev["description"],
                        "instance_id": usb_dev["instance_id"],
                        "current_driver": usb_dev["driver"],
                        "needed_backend": sb_backend,
                        "advice": advice,
                    })

        # Summary note if USB devices found but seabreeze sees fewer
        n_usb = len(report["usb_devices"])
        n_sb = len(report["seabreeze_devices"])
        if n_usb > 0 and n_sb < n_usb:
            report["notes"].append(
                f"Windows sees {n_usb} Ocean Optics USB device(s), "
                f"but seabreeze only recognises {n_sb}.\n"
                "The missing device(s) likely have the wrong USB driver bound."
            )

        return report

    def _lazy_import_seabreeze(self):
        """Import seabreeze once and stash the module reference."""
        if self._sb is not None:
            return
        try:
            import seabreeze
            try:
                seabreeze.use("cseabreeze")
                logger.info("Using cseabreeze backend")
            except Exception:
                seabreeze.use("pyseabreeze")
                logger.info("cseabreeze unavailable, using pyseabreeze backend")
            self._sb = seabreeze
        except ImportError:
            raise SpectrometerError(
                "python-seabreeze is not installed.\n"
                "Install it with: pip install seabreeze\n"
                "Then run: seabreeze_os_setup  (for USB driver setup on Windows)"
            )


# ═══════════════════════════════════════════════════════════════════════
#  Thorlabs CCS-series backend  (TLCCS_64.dll via ctypes)
# ═══════════════════════════════════════════════════════════════════════

# Product-ID → model name mapping used for VISA resource strings and display
_CCS_PRODUCT_IDS: dict[int, str] = {
    0x8081: "CCS100",
    0x8083: "CCS125",
    0x8085: "CCS150",
    0x8087: "CCS175",
    0x8089: "CCS200",
}

# All CCS models have 3648 pixels
_CCS_NUM_PIXELS = 3648

# Status bit that indicates scan data is ready (from TLCCS.h)
_TLCCS_STATUS_SCAN_TRANSFER = 0x0010


class ThorlabsCCSModule(SpectrometerBase):
    """
    Thorlabs CCS-series spectrometer backend.
    
    All CCS models (CCS100/125/150/175/200) share the same 3648-pixel Toshiba
    TCD1304 linear CCD and the same TLCCS DLL interface.
    
    The DLL is loaded lazily via ctypes; the user needs:
      - ThorSpectra software installed (provides ``TLCCS_64.dll``)
      - NI-VISA runtime (usually bundled with ThorSpectra)
    
    CCS specifics vs Ocean Optics:
      - Intensity is normalised **0.0 – 1.0** (not raw ADC counts).
      - Integration time is passed to the DLL in **seconds** (we convert from µs).
      - No external hardware trigger is exposed in the DLL API.
      - Scan is polled: call ``startScan`` then poll ``getDeviceStatus`` for the
        data-ready bit before calling ``getScanData``.
    """

    # Integration time limits common to all CCS models
    _INT_TIME_MIN_US = 10           # 10 µs  →  1e-5 s
    _INT_TIME_MAX_US = 60_000_000   # 60 s

    def __init__(self):
        self._lib = None               # ctypes CDLL handle
        self._handle = None            # ViSession integer
        self._wavelengths: np.ndarray | None = None
        self._capabilities = DeviceCapabilities()
        self._integration_time_us: int = 100_000  # 100 ms default
        self._current_trigger_mode: int = 0
        self._simulated: bool = False
        self._spec = None              # only used in simulation mode
        self._model_name: str = "CCS"
        self._serial: str = "N/A"

    # ─── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if self._simulated:
            return self._spec is not None and self._spec.is_open
        return self._handle is not None

    @property
    def capabilities(self) -> DeviceCapabilities:
        return self._capabilities

    @property
    def integration_time_us(self) -> int:
        return self._integration_time_us

    @property
    def current_trigger_mode(self) -> int:
        return self._current_trigger_mode

    @property
    def model(self) -> str:
        return self._model_name if self.is_connected else "N/A"

    @property
    def serial_number(self) -> str:
        return self._serial if self.is_connected else "N/A"

    # ─── Connection ────────────────────────────────────────────────────

    def connect(self, device_index: int = 0) -> str:
        """Discover Thorlabs CCS spectrometers and open the one at *device_index*."""
        self._simulated = False
        try:
            self._load_dll()
        except SpectrometerError:
            # DLL not installed → treat as "no device" so the GUI
            # shows the friendly simulation-offer dialog.
            raise NoDeviceError(
                "No Thorlabs CCS spectrometer found.\n\n"
                "The Thorlabs driver (TLCCS_64.dll) is not installed.\n"
                "If you have a CCS spectrometer, install ThorSpectra from:\n"
                "  https://www.thorlabs.com → Software → CCS"
            )

        devices = self.list_available_devices()
        if not devices:
            raise NoDeviceError(
                "No Thorlabs CCS spectrometer found.\n\n"
                "Check that:\n"
                "  1. The CCS spectrometer is plugged in via USB\n"
                "  2. ThorSpectra software is installed\n"
                "  3. The READY LED on the device is green\n"
                "  4. No other software is using the device"
            )

        if device_index >= len(devices):
            raise SpectrometerError(
                f"Device index {device_index} out of range — "
                f"only {len(devices)} device(s) found."
            )

        _model, _serial, resource_name = devices[device_index]
        return self._open_resource(resource_name)

    def connect_with_resource(self, resource_name: str) -> str:
        """
        Open a CCS spectrometer using an explicit VISA resource string.
        
        Example::
        
            "USB0::0x1313::0x8087::M00123456::RAW"
        """
        self._simulated = False
        self._load_dll()
        return self._open_resource(resource_name)

    def _open_resource(self, resource_name: str) -> str:
        """Shared logic for opening a CCS device by VISA resource string."""
        from ctypes import c_ulong, c_double, byref

        handle = c_ulong(0)
        err = self._lib.tlccs_init(
            resource_name.encode() if isinstance(resource_name, str) else resource_name,
            1,   # id_query
            1,   # reset
            byref(handle),
        )
        if err != 0:
            raise SpectrometerError(
                f"Failed to initialise CCS spectrometer.\n"
                f"Resource: {resource_name}\n"
                f"TLCCS error code: {err}\n\n"
                "Check that the serial number and product ID are correct."
            )

        self._handle = handle.value

        # Read wavelength array
        wl_array = (c_double * _CCS_NUM_PIXELS)()
        c_min = c_double(0)
        c_max = c_double(0)
        self._lib.tlccs_getWavelengthData(
            self._handle, 0, wl_array, byref(c_min), byref(c_max)
        )
        self._wavelengths = np.array(wl_array[:], dtype=np.float64)

        # Identify model from resource string
        self._model_name = "CCS"
        for pid, name in _CCS_PRODUCT_IDS.items():
            pid_hex = f"0x{pid:04X}"
            if pid_hex in resource_name.upper():
                self._model_name = name
                break

        # Serial from resource string  (pattern …::M00123456::RAW)
        self._serial = "N/A"
        parts = resource_name.replace("'", "").split("::")
        for part in parts:
            if part.startswith("M") and part[1:].isdigit():
                self._serial = part
                break

        # Set default integration time
        int_sec = c_double(self._integration_time_us * 1e-6)
        self._lib.tlccs_setIntegrationTime(self._handle, int_sec)

        # ── Populate capabilities ──────────────────────────────────
        caps = DeviceCapabilities()
        caps.brand = "thorlabs"
        caps.model = self._model_name
        caps.serial_number = self._serial
        caps.pixel_count = _CCS_NUM_PIXELS
        caps.wavelength_min = round(float(self._wavelengths[0]), 1)
        caps.wavelength_max = round(float(self._wavelengths[-1]), 1)
        caps.max_intensity = 1.0   # normalised
        caps.integration_time_min_us = self._INT_TIME_MIN_US
        caps.integration_time_max_us = self._INT_TIME_MAX_US
        caps.trigger_modes = {"normal": 0}   # no external trigger on CCS
        caps.supports_dark_correction = False
        caps.supports_nonlinearity_correction = False
        self._capabilities = caps

        # ── Verification read ──────────────────────────────────────
        try:
            test = self.get_intensities()
            if len(test) != _CCS_NUM_PIXELS:
                logger.warning(
                    f"CCS verification: expected {_CCS_NUM_PIXELS} pixels, got {len(test)}"
                )
            else:
                logger.info(f"CCS verification read OK: {_CCS_NUM_PIXELS} pixels")
        except Exception as e:
            self._close_handle()
            raise SpectrometerError(
                f"CCS opened but verification read failed:\n{e}"
            )

        # ── Build status string ────────────────────────────────────
        status_lines = [
            f"Connected: {caps.model} (S/N: {caps.serial_number})",
            f"Pixels: {caps.pixel_count} | "
            f"Range: {caps.wavelength_min}–{caps.wavelength_max} nm",
            f"Integration: {caps.integration_time_min_us / 1000.0}–"
            f"{caps.integration_time_max_us / 1000.0} ms",
            f"Intensity: normalised 0.0–1.0",
        ]
        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def connect_simulated(self, profile_name: str = "CCS175") -> str:
        """Open a simulated CCS spectrometer."""
        profile = SIMULATION_PROFILES.get(profile_name, SIMULATION_PROFILES.get("CCS175"))
        if profile is None:
            profile = SIMULATION_PROFILES["Generic"]
        self._spec = _SimulatedSpectrometer(profile)
        self._wavelengths = self._spec.wavelengths()
        self._integration_time_us = 100_000
        self._current_trigger_mode = 0
        self._simulated = True

        caps = DeviceCapabilities()
        caps.brand = "simulated"
        caps.model = self._spec.model
        caps.serial_number = self._spec.serial_number
        caps.pixel_count = self._spec._pixels
        caps.wavelength_min = round(self._spec._wl_min, 1)
        caps.wavelength_max = round(self._spec._wl_max, 1)
        caps.max_intensity = float(self._spec._max_intensity)
        caps.integration_time_min_us = self._spec._int_min_us
        caps.integration_time_max_us = self._spec._int_max_us
        caps.trigger_modes = dict(self._spec._trigger_modes)
        caps.supports_dark_correction = False
        caps.supports_nonlinearity_correction = False
        self._capabilities = caps
        self._model_name = caps.model
        self._serial = caps.serial_number

        status_lines = [
            f"Connected: {caps.model} (S/N: {caps.serial_number}) [SIMULATED]",
            f"Pixels: {caps.pixel_count} | Range: {caps.wavelength_min}–{caps.wavelength_max} nm",
            f"Integration: {caps.integration_time_min_us / 1000.0}–"
            f"{caps.integration_time_max_us / 1000.0} ms",
            f"Intensity: normalised 0.0–1.0",
        ]
        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def disconnect(self):
        """Close the CCS spectrometer."""
        if self._simulated and self._spec:
            self._spec.close()
            self._spec = None
        elif self._handle is not None:
            self._close_handle()

        self._wavelengths = None
        self._current_trigger_mode = 0
        self._simulated = False
        self._capabilities = DeviceCapabilities()
        logger.info("CCS spectrometer disconnected.")

    # ─── Configuration ─────────────────────────────────────────────────

    def set_integration_time(self, microseconds: int):
        if not self.is_connected:
            raise SpectrometerError("CCS spectrometer not connected.")

        microseconds = max(self._INT_TIME_MIN_US,
                           min(microseconds, self._INT_TIME_MAX_US))

        if self._simulated:
            self._spec.integration_time_micros(microseconds)
        else:
            from ctypes import c_double
            self._lib.tlccs_setIntegrationTime(
                self._handle, c_double(microseconds * 1e-6)
            )

        self._integration_time_us = microseconds
        logger.info(f"CCS integration time set to {microseconds} µs")

    def set_trigger_mode(self, mode: int):
        """CCS has no external trigger — only mode 0 (normal) is valid."""
        if mode != 0:
            raise ValueError(
                f"Thorlabs CCS does not support trigger mode {mode}. "
                "Only normal mode (0) is available."
            )
        self._current_trigger_mode = 0

    # ─── Data Acquisition ──────────────────────────────────────────────

    def get_wavelengths(self) -> np.ndarray:
        if not self.is_connected:
            raise SpectrometerError("CCS spectrometer not connected.")
        return self._wavelengths.copy()

    def get_intensities(self, correct_dark_counts: bool = False,
                         correct_nonlinearity: bool = False) -> np.ndarray:
        """
        Acquire one spectrum from the CCS.
        
        Returns an array of 3648 doubles in the range 0.0–1.0.
        ``correct_dark_counts`` and ``correct_nonlinearity`` are accepted
        for interface compatibility but ignored (CCS DLL doesn't support them).
        """
        if not self.is_connected:
            raise SpectrometerError("CCS spectrometer not connected.")

        if self._simulated:
            return self._spec.intensities()

        from ctypes import c_double, c_int, byref

        # Start scan
        err = self._lib.tlccs_startScan(self._handle)
        if err != 0:
            raise SpectrometerError(f"CCS startScan failed (error {err})")

        # Poll for data ready
        status = c_int(0)
        timeout_count = 0
        max_polls = int(self._integration_time_us / 1000) + 5000  # generous timeout
        while (status.value & _TLCCS_STATUS_SCAN_TRANSFER) == 0:
            self._lib.tlccs_getDeviceStatus(self._handle, byref(status))
            time.sleep(0.001)
            timeout_count += 1
            if timeout_count > max_polls:
                raise SpectrometerError(
                    "CCS scan timed out — no data received. "
                    "Check integration time or device connection."
                )

        # Read scan data
        data = (c_double * _CCS_NUM_PIXELS)()
        err = self._lib.tlccs_getScanData(self._handle, data)
        if err != 0:
            raise SpectrometerError(f"CCS getScanData failed (error {err})")

        return np.array(data[:], dtype=np.float64)

    # ─── Device discovery ──────────────────────────────────────────────

    def list_available_devices(self) -> list:
        """
        Scan for connected CCS spectrometers using NI-VISA resource manager.
        
        Returns a list of (model_name, serial_number, resource_string) tuples.
        """
        self._load_dll()

        try:
            import ctypes
            from ctypes import c_ulong, byref, create_string_buffer

            # Try loading NI-VISA to enumerate USB resources
            try:
                visa_lib = ctypes.cdll.LoadLibrary("visa64.dll")
            except OSError:
                try:
                    visa_lib = ctypes.cdll.LoadLibrary("visa32.dll")
                except OSError:
                    logger.warning("NI-VISA not found — cannot auto-discover CCS devices.")
                    return []

            rm = c_ulong(0)
            err = visa_lib.viOpenDefaultRM(byref(rm))
            if err != 0:
                return []

            found = []
            for pid, model_name in _CCS_PRODUCT_IDS.items():
                pattern = f"USB0::0x1313::0x{pid:04X}::?*::RAW".encode()
                find_list = c_ulong(0)
                count = c_ulong(0)
                rsc = create_string_buffer(512)

                err = visa_lib.viFindRsrc(rm, pattern, byref(find_list), byref(count), rsc)
                if err == 0 and count.value > 0:
                    resource_str = rsc.value.decode()
                    serial = "N/A"
                    parts = resource_str.split("::")
                    for part in parts:
                        if part.startswith("M") and len(part) > 1:
                            serial = part
                            break
                    found.append((model_name, serial, resource_str))

                    # If more than one of same model
                    for _ in range(count.value - 1):
                        err2 = visa_lib.viFindNext(find_list, rsc)
                        if err2 == 0:
                            resource_str = rsc.value.decode()
                            serial = "N/A"
                            parts = resource_str.split("::")
                            for part in parts:
                                if part.startswith("M") and len(part) > 1:
                                    serial = part
                                    break
                            found.append((model_name, serial, resource_str))

                    visa_lib.viClose(find_list)

            visa_lib.viClose(rm)
            return found

        except Exception as e:
            logger.debug(f"CCS device discovery failed: {e}")
            return []

    # ─── Internal helpers ──────────────────────────────────────────────

    def _load_dll(self):
        """Load the TLCCS_64.dll if not already loaded."""
        if self._lib is not None:
            return
        import ctypes
        import os as _os

        # Common install locations
        search_paths = [
            r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
            r"C:\Program Files (x86)\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
        ]
        for path in search_paths:
            if _os.path.isfile(path):
                try:
                    self._lib = ctypes.cdll.LoadLibrary(path)
                    logger.info(f"Loaded TLCCS DLL from {path}")
                    return
                except OSError as e:
                    logger.debug(f"Failed to load {path}: {e}")

        # Fallback: let ctypes search in system PATH
        try:
            self._lib = ctypes.cdll.LoadLibrary("TLCCS_64.dll")
            logger.info("Loaded TLCCS_64.dll from system PATH")
        except OSError:
            raise SpectrometerError(
                "Thorlabs TLCCS_64.dll not found.\n\n"
                "Install ThorSpectra from:\n"
                "  https://www.thorlabs.com/software_pages/ViewSoftwarePage.cfm?Code=CCS\n\n"
                "The DLL is expected at:\n"
                "  C:\\Program Files\\IVI Foundation\\VISA\\Win64\\Bin\\TLCCS_64.dll"
            )

    @classmethod
    def diagnose(cls) -> dict:
        """
        Run a comprehensive diagnostic scan for Thorlabs CCS spectrometers.

        Returns a dict with::

            {
                "backend": "ThorlabsCCSModule",
                "dll_found": bool,
                "dll_path": str | None,
                "visa_installed": bool,
                "visa_resources": [...],
                "usb_devices": [...],
                "notes": [str, ...],
            }
        """
        import os as _os

        report: dict = {
            "backend": "ThorlabsCCSModule",
            "dll_found": False,
            "dll_path": None,
            "visa_installed": False,
            "visa_resources": [],
            "usb_devices": [],
            "notes": [],
        }

        # 1. USB bus scan for Thorlabs VID
        try:
            all_usb = scan_usb_spectrometers()
            report["usb_devices"] = [
                d for d in all_usb if d["brand"] == "thorlabs"
            ]
        except Exception as e:
            report["notes"].append(f"USB bus scan error: {e}")

        # 2. Check DLL
        search_paths = [
            r"C:\Program Files\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
            r"C:\Program Files (x86)\IVI Foundation\VISA\Win64\Bin\TLCCS_64.dll",
        ]
        for path in search_paths:
            if _os.path.isfile(path):
                report["dll_found"] = True
                report["dll_path"] = path
                break

        if not report["dll_found"]:
            # Try system PATH
            import shutil
            found = shutil.which("TLCCS_64.dll")
            if found:
                report["dll_found"] = True
                report["dll_path"] = found

        if not report["dll_found"]:
            report["notes"].append(
                "TLCCS_64.dll not found.\n"
                "Install ThorSpectra from thorlabs.com to get the driver DLL."
            )

        # 3. Check NI-VISA and enumerate resources
        try:
            import ctypes
            from ctypes import c_ulong, byref, create_string_buffer

            visa_lib = None
            for dll_name in ("visa64.dll", "visa32.dll"):
                try:
                    visa_lib = ctypes.cdll.LoadLibrary(dll_name)
                    break
                except OSError:
                    continue

            if visa_lib is None:
                report["notes"].append(
                    "NI-VISA runtime not found (visa64.dll / visa32.dll).\n"
                    "NI-VISA is required to communicate with Thorlabs CCS devices.\n"
                    "It is usually bundled with ThorSpectra."
                )
            else:
                report["visa_installed"] = True
                rm = c_ulong(0)
                err = visa_lib.viOpenDefaultRM(byref(rm))
                if err == 0:
                    for pid, model_name in _CCS_PRODUCT_IDS.items():
                        pattern = f"USB0::0x1313::0x{pid:04X}::?*::RAW".encode()
                        find_list = c_ulong(0)
                        count = c_ulong(0)
                        rsc = create_string_buffer(512)
                        err2 = visa_lib.viFindRsrc(
                            rm, pattern, byref(find_list), byref(count), rsc
                        )
                        if err2 == 0 and count.value > 0:
                            resource_str = rsc.value.decode()
                            report["visa_resources"].append({
                                "model": model_name,
                                "resource": resource_str,
                            })
                            for _ in range(count.value - 1):
                                if visa_lib.viFindNext(find_list, rsc) == 0:
                                    report["visa_resources"].append({
                                        "model": model_name,
                                        "resource": rsc.value.decode(),
                                    })
                            visa_lib.viClose(find_list)
                    visa_lib.viClose(rm)

                    if not report["visa_resources"]:
                        report["notes"].append(
                            "NI-VISA is installed but found no CCS VISA resources.\n"
                            "Check that the CCS is plugged in and ThorSpectra can see it."
                        )
                else:
                    report["notes"].append(
                        f"viOpenDefaultRM failed (error {err}). NI-VISA may be corrupt."
                    )

        except Exception as e:
            report["notes"].append(f"VISA diagnostic error: {e}")

        # 4. Summary
        n_usb = len(report["usb_devices"])
        n_visa = len(report["visa_resources"])
        if n_usb > 0 and n_visa == 0:
            report["notes"].append(
                f"Windows sees {n_usb} Thorlabs USB device(s) on the bus, "
                "but VISA cannot find them.\n"
                "This usually means the VISA/ThorSpectra driver is not installed "
                "or the device needs to be power-cycled."
            )

        return report

    def _close_handle(self):
        """Close the DLL session handle."""
        if self._lib is not None and self._handle is not None:
            try:
                self._lib.tlccs_close(self._handle)
            except Exception as e:
                logger.warning(f"Error closing CCS handle: {e}")
            self._handle = None
