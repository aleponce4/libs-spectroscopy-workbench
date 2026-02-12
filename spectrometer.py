# spectrometer.py - Hardware abstraction layer for optical spectrometers.
# Uses python-seabreeze with lazy loading — seabreeze is only imported when connect() is called.
# Includes a built-in simulation mode for testing without hardware.
#
# Architecture note:
#   SpectrometerModule is the concrete class for Ocean Optics / Ocean Insight
#   spectrometers via python-seabreeze.  It is designed so that a future
#   SpectrometerBase ABC can be extracted if support for other brands
#   (Avantes, Thorlabs, Broadcom, StellarNet …) is ever needed.
#   Any code that *consumes* a spectrometer should depend only on the public
#   interface of SpectrometerModule (properties + methods), making it easy to
#   swap in an alternative backend later.

import numpy as np
import time
import logging

logger = logging.getLogger(__name__)


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

        spectrum = np.clip(spectrum, 0, self._max_intensity)
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
_EXTERNAL_TRIGGER_MODE_NAMES = {"HARDWARE", "EDGE", "OBP_EXTERNAL", "OBP_EDGE"}


def _build_trigger_map_from_seabreeze(spec) -> dict[str, int]:
    """
    Inspect a real seabreeze Spectrometer to build a semantic trigger-mode map.
    
    Returns a dict like {"normal": 0, "external": 3} (or without "external"
    if the device has no external trigger support).
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

                if mode_name in _NORMAL_MODE_NAMES:
                    trigger_map["normal"] = mode_int
                elif mode_name in _EXTERNAL_TRIGGER_MODE_NAMES:
                    trigger_map["external"] = mode_int
                # Also keep original mode names for power-users
                trigger_map[mode_name.lower()] = mode_int

    except Exception as e:
        logger.debug(f"Could not inspect trigger modes from device: {e}")

    # Fallback: at minimum ensure "normal" = 0
    if "normal" not in trigger_map:
        trigger_map["normal"] = 0

    return trigger_map


# ═══════════════════════════════════════════════════════════════════════
#  SpectrometerModule — main public interface
# ═══════════════════════════════════════════════════════════════════════

class SpectrometerModule:
    """
    Hardware abstraction layer for Ocean Optics spectrometers via python-seabreeze.
    
    Model-agnostic:  all hardware-specific parameters (pixel count, trigger modes,
    integration-time limits, max intensity …) are queried from the device at connect-
    time and exposed through the ``capabilities`` property.

    Supports any Ocean Optics / Ocean Insight spectrometer that python-seabreeze
    recognises (USB2000, USB4000, HR4000, QEPro, HDX, Flame, Ocean ST, …).
    
    Architecture:
        A future ``SpectrometerBase`` ABC can be extracted from this class so that
        other brands (Avantes, Thorlabs, StellarNet …) can share the same GUI code.
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

    # ─── Context Manager ───────────────────────────────────────────────

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __del__(self):
        self.disconnect()
