# spectrometer.py - Hardware abstraction layer for Ocean Optics USB4000 spectrometer.
# Uses python-seabreeze with lazy loading — seabreeze is only imported when connect() is called.
# Includes a built-in simulation mode for testing without hardware.

import numpy as np
import time
import logging

logger = logging.getLogger(__name__)


class SpectrometerError(Exception):
    """Custom exception for spectrometer-related errors."""
    pass


class NoDeviceError(SpectrometerError):
    """Raised specifically when no spectrometer hardware is found.
    The GUI can catch this to offer simulation mode."""
    pass


# ═══════════════════════════════════════════════════════════════════════
#  Simulated Spectrometer (for testing without hardware)
# ═══════════════════════════════════════════════════════════════════════

class _SimulatedSpectrometer:
    """
    Fake spectrometer that mimics the USB4000 interface.
    Generates realistic-looking LIBS spectra with noise that varies on each call.
    """

    def __init__(self):
        self.is_open = True
        self.model = "USB4000-SIM"
        self.serial_number = "SIM00001"
        self._integration_time_us = 100_000
        self._trigger_mode = 0

        # USB4000 has 3648 pixels, wavelength range ~200–1100 nm
        self._pixels = 3648
        self._wavelengths = np.linspace(200.0, 1100.0, self._pixels)

        # Define some synthetic LIBS emission lines (common elements)
        # (center_nm, relative_intensity, width_nm)
        self._emission_lines = [
            # Iron (Fe) lines
            (238.20, 0.45, 0.15), (239.56, 0.50, 0.15), (240.49, 0.35, 0.15),
            (248.33, 0.40, 0.15), (252.28, 0.38, 0.15), (259.94, 0.55, 0.15),
            (271.44, 0.30, 0.15), (273.95, 0.42, 0.15), (275.57, 0.35, 0.15),
            (358.12, 0.60, 0.18), (371.99, 0.75, 0.18), (373.49, 0.65, 0.18),
            (374.56, 0.50, 0.18), (382.04, 0.55, 0.18), (385.99, 0.80, 0.18),
            (404.58, 0.45, 0.18),
            # Calcium (Ca) lines
            (393.37, 0.90, 0.20), (396.85, 0.85, 0.20),
            (422.67, 0.70, 0.18), (445.48, 0.30, 0.15),
            # Sodium (Na) lines
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

    def intensities(self):
        # Simulate trigger delay
        if self._trigger_mode == 3:
            # Simulate waiting for trigger (1–3 seconds random delay)
            delay = np.random.uniform(1.0, 3.0)
            time.sleep(delay)

        # Build spectrum: baseline + peaks + noise
        baseline = 250 + 50 * np.sin(self._wavelengths / 200.0)  # Gentle baseline

        # Scale factor based on integration time
        scale = self._integration_time_us / 100_000.0

        spectrum = baseline.copy()

        # Add emission lines as Gaussians with slight random jitter per call
        for center, rel_intensity, width in self._emission_lines:
            intensity = rel_intensity * 3500 * scale  # Max ~3500 counts
            # Add slight intensity jitter (±15%) to make it look alive
            intensity *= np.random.uniform(0.85, 1.15)
            width_jitter = width * np.random.uniform(0.9, 1.1)
            spectrum += intensity * np.exp(-0.5 * ((self._wavelengths - center) / width_jitter) ** 2)

        # Add realistic noise (shot noise + readout noise)
        shot_noise = np.sqrt(np.maximum(spectrum, 0)) * np.random.randn(self._pixels) * 0.5
        readout_noise = np.random.randn(self._pixels) * 8
        spectrum += shot_noise + readout_noise

        # Clamp to realistic range
        spectrum = np.clip(spectrum, 0, 65535)

        return spectrum

    def integration_time_micros(self, us):
        self._integration_time_us = us

    def trigger_mode(self, mode):
        self._trigger_mode = mode

    def close(self):
        self.is_open = False


class SpectrometerModule:
    """
    Wraps python-seabreeze to provide a clean interface for the USB4000 spectrometer.
    
    Supports two trigger modes:
        - Mode 0 (Normal): Free-running / software trigger — for live view.
        - Mode 3 (External Hardware Edge): Waits for external TTL trigger — for LIBS laser sync.
    
    Seabreeze is imported lazily inside connect() so that the analysis side of the app
    never needs seabreeze installed to function.
    """

    # Trigger mode constants
    TRIGGER_NORMAL = 0
    TRIGGER_EXTERNAL_HARDWARE_EDGE = 3

    def __init__(self):
        self._spec = None
        self._sb = None  # seabreeze module reference
        self._wavelengths = None
        self._current_trigger_mode = self.TRIGGER_NORMAL
        self._integration_time_us = 100_000  # 100 ms default
        self._simulated = False

    # ─── Properties ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Check if a spectrometer is currently connected and open."""
        return self._spec is not None and self._spec.is_open

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

    def connect(self) -> str:
        """
        Lazy-import seabreeze, discover devices, and open the first available spectrometer.
        Performs a verification read to confirm communication is working.
        
        Returns:
            A multi-line status string with device details.
            
        Raises:
            SpectrometerError: If seabreeze is not installed, no device is found,
                               or the connection fails.
        """
        # Lazy import — try cseabreeze first, fall back to pyseabreeze
        try:
            import seabreeze
            try:
                seabreeze.use("cseabreeze")
                logger.info("Using cseabreeze backend")
            except Exception:
                seabreeze.use("pyseabreeze")
                logger.info("cseabreeze unavailable, using pyseabreeze backend")
            from seabreeze.spectrometers import Spectrometer, list_devices
            self._sb = seabreeze
        except ImportError:
            raise SpectrometerError(
                "python-seabreeze is not installed.\n"
                "Install it with: pip install seabreeze\n"
                "Then run: seabreeze_os_setup  (for USB driver setup on Windows)"
            )

        # Discover devices
        try:
            devices = list_devices()
        except Exception as e:
            raise SpectrometerError(f"Error scanning for spectrometers: {e}")

        if not devices:
            raise NoDeviceError(
                "No spectrometer found.\n\n"
                "Check that:\n"
                "  1. The USB4000 is plugged in\n"
                "  2. The USB driver is installed (run seabreeze_os_setup)\n"
                "  3. No other software (OceanView) is using the device"
            )

        # Log all discovered devices
        logger.info(f"Found {len(devices)} device(s):")
        for i, dev in enumerate(devices):
            logger.info(f"  [{i}] {dev}")

        # Open the first device
        try:
            self._spec = Spectrometer(devices[0])
        except Exception as e:
            raise SpectrometerError(f"Failed to open spectrometer: {e}")

        # Cache wavelengths (they never change for a given device)
        self._wavelengths = self._spec.wavelengths()

        # Query device capabilities
        pixel_count = self._spec.pixels
        wl_min = round(self._wavelengths[0], 1)
        wl_max = round(self._wavelengths[-1], 1)
        try:
            int_limits = self._spec.integration_time_micros_limits
            int_min_us, int_max_us = int_limits
            int_min_ms = int_min_us / 1000.0
            int_max_ms = int_max_us / 1000.0
        except Exception:
            int_min_ms, int_max_ms = None, None
        try:
            max_intensity = self._spec.max_intensity
        except Exception:
            max_intensity = None

        # Set default integration time
        self.set_integration_time(self._integration_time_us)

        # Ensure we start in normal trigger mode
        self.set_trigger_mode(self.TRIGGER_NORMAL)

        # Verification read — confirm the device actually responds
        try:
            test_spectrum = self._spec.intensities()
            if len(test_spectrum) != pixel_count:
                logger.warning(f"Verification: expected {pixel_count} pixels, got {len(test_spectrum)}")
            else:
                logger.info(f"Verification read OK: {pixel_count} pixels returned")
        except Exception as e:
            # Device opened but can't read — close and report
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

        # Build informative status string
        status_lines = [f"Connected: {self.model} (S/N: {self.serial_number})"]
        status_lines.append(f"Pixels: {pixel_count} | Range: {wl_min}–{wl_max} nm")
        if int_min_ms is not None:
            status_lines.append(f"Integration: {int_min_ms}–{int_max_ms} ms")
        if max_intensity is not None:
            status_lines.append(f"Max intensity: {max_intensity:.0f}")

        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def connect_simulated(self) -> str:
        """
        Connect to the built-in simulated spectrometer.
        Useful for testing the GUI without real hardware.
        
        Returns:
            A multi-line status string.
        """
        self._spec = _SimulatedSpectrometer()
        self._wavelengths = self._spec.wavelengths()
        self._integration_time_us = 100_000
        self._current_trigger_mode = self.TRIGGER_NORMAL
        self._simulated = True

        wl_min = round(self._wavelengths[0], 1)
        wl_max = round(self._wavelengths[-1], 1)

        status_lines = [
            f"Connected: {self.model} (S/N: {self.serial_number}) [SIMULATED]",
            f"Pixels: {self._spec._pixels} | Range: {wl_min}–{wl_max} nm",
            "Integration: 0.01–65535.0 ms",
        ]
        status = "\n".join(status_lines)
        logger.info(status)
        return status

    def disconnect(self):
        """Safely close the spectrometer connection."""
        if self._spec is not None:
            try:
                # Ensure we return to normal trigger mode before closing
                if self._current_trigger_mode != self.TRIGGER_NORMAL:
                    try:
                        self._spec.trigger_mode(self.TRIGGER_NORMAL)
                    except Exception:
                        pass
                self._spec.close()
                logger.info("Spectrometer disconnected.")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self._spec = None
                self._wavelengths = None
                self._current_trigger_mode = self.TRIGGER_NORMAL

    # ─── Configuration ─────────────────────────────────────────────────

    def set_integration_time(self, microseconds: int):
        """
        Set the integration time in microseconds.
        
        Args:
            microseconds: Integration time (USB4000 range: 10 to 65,535,000 µs).
        """
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        microseconds = max(10, min(microseconds, 65_535_000))
        try:
            self._spec.integration_time_micros(microseconds)
            self._integration_time_us = microseconds
            logger.info(f"Integration time set to {microseconds} µs")
        except Exception as e:
            raise SpectrometerError(f"Failed to set integration time: {e}")

    def set_trigger_mode(self, mode: int):
        """
        Set the trigger mode.
        
        Args:
            mode: 0 for Normal (free-running), 3 for External Hardware Edge Trigger.
        """
        if not self.is_connected:
            raise SpectrometerError("Spectrometer not connected.")

        if mode not in (self.TRIGGER_NORMAL, self.TRIGGER_EXTERNAL_HARDWARE_EDGE):
            raise ValueError(f"Invalid trigger mode: {mode}. Use 0 (Normal) or 3 (External HW Edge).")

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
        
        In Normal mode (0), returns immediately after integration.
        In External HW Edge mode (3), BLOCKS until a trigger signal is received.
        
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
                correct_nonlinearity=correct_nonlinearity
            )
        except Exception as e:
            raise SpectrometerError(f"Acquisition error: {e}")

    def get_spectrum(self) -> tuple:
        """
        Convenience method that returns (wavelengths, intensities) as a tuple.
        """
        return self.get_wavelengths(), self.get_intensities()

    # ─── Context Manager ───────────────────────────────────────────────

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __del__(self):
        self.disconnect()
