# acquisition_worker.py - Background thread for spectrometer data acquisition.
# Handles two states: LIVE (continuous polling) and ARMED (wait for hardware trigger).
# Communicates with the GUI via a thread-safe queue.

import threading
import queue
import time
import numpy as np
import logging
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future

logger = logging.getLogger(__name__)


class AcquisitionMessage:
    """Message types sent from the worker thread to the GUI."""
    SPECTRUM = "spectrum"          # New spectrum data available
    STATUS = "status"              # Status text update
    ERROR = "error"                # An error occurred
    ARMED = "armed"                # Worker is armed and waiting for trigger
    CAPTURED = "captured"          # A triggered capture completed
    STOPPED = "stopped"            # Worker has stopped
    SAVE_COMPLETE = "save_complete"  # Auto-save finished
    IDLE = "idle"                      # Worker returned to idle (buttons should reset)


class AcquisitionWorker(threading.Thread):
    """
    Background thread that controls spectrometer acquisition.
    
    States:
        IDLE  - Not acquiring. Thread is alive but sleeping.
        LIVE  - Continuous polling (Normal trigger mode). For live preview.
        ARMED - Waiting for external hardware trigger. Blocks until laser fires.
    
    Trigger mode integers are read from ``spec.capabilities`` at runtime
    so they match whatever spectrometer model is connected.
    
    All GUI updates are pushed to a queue.Queue and must be consumed by the
    main thread using root.after() polling.
    """

    # States
    STATE_IDLE = "IDLE"
    STATE_LIVE = "LIVE"
    STATE_ARMED = "ARMED"
    STATE_TEST = "TEST"

    def __init__(self, spectrometer_module):
        """
        Args:
            spectrometer_module: A connected SpectrometerBase instance
                (SpectrometerModule for Ocean Optics, ThorlabsCCSModule for Thorlabs).
        """
        super().__init__(daemon=True, name="AcquisitionWorker")
        self.spec = spectrometer_module
        self.message_queue = queue.Queue()

        # State management
        self._state = self.STATE_IDLE
        self._state_lock = threading.Lock()

        # Control events
        self._stop_event = threading.Event()
        self._state_change_event = threading.Event()

        # Auto-save configuration
        self.auto_save_enabled = True
        self.save_directory = os.path.join(os.path.expanduser("~"), "LIBS_Data")
        self.sample_name = "Sample"
        self._shot_index = 0

        # Live view rate limiting
        self.live_poll_interval = 0.05  # 50 ms between live polls

        # Averaging
        self.averages = 1  # Number of spectra to average in LIVE mode

        # Correction flags (passed to spectrometer.get_intensities)
        self.correct_dark_counts = False
        self.correct_nonlinearity = False

    # ─── State Management ──────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: str):
        with self._state_lock:
            old_state = self._state
            self._state = new_state
        logger.info(f"Worker state: {old_state} → {new_state}")
        self._state_change_event.set()

    # ─── Control Methods (called from GUI thread) ─────────────────────

    def start_live(self):
        """Switch to LIVE mode (continuous spectrum polling)."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return
        try:
            normal_mode = self.spec.capabilities.normal_trigger_mode
            self.spec.set_trigger_mode(normal_mode)
        except Exception as e:
            self._send(AcquisitionMessage.ERROR, str(e))
            return
        self._set_state(self.STATE_LIVE)
        self._send(AcquisitionMessage.STATUS, "Live view started")

    def arm_trigger(self):
        """Switch to ARMED mode (wait for external hardware trigger)."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return

        ext_mode = self.spec.capabilities.external_trigger_mode
        if ext_mode is None:
            self._send(AcquisitionMessage.ERROR,
                       f"{self.spec.model} does not support an external hardware trigger.")
            return

        try:
            self.spec.set_trigger_mode(ext_mode)
        except Exception as e:
            self._send(AcquisitionMessage.ERROR, str(e))
            return
        self._set_state(self.STATE_ARMED)
        self._send(AcquisitionMessage.STATUS, "Armed — waiting for trigger...")
        self._send(AcquisitionMessage.ARMED, None)

    def go_idle(self):
        """Return to IDLE state. Stops live view or disarms trigger."""
        self._set_state(self.STATE_IDLE)
        # Return spectrometer to normal mode so it doesn't block
        if self.spec.is_connected:
            try:
                normal_mode = self.spec.capabilities.normal_trigger_mode
                self.spec.set_trigger_mode(normal_mode)
            except Exception:
                pass
        self._send(AcquisitionMessage.STATUS, "Idle")

    def test_trigger(self):
        """Queue a test capture to run on the worker thread.
        Does a normal-mode read pushed through the full capture pipeline
        (plot, shot counter, auto-save) to verify software is working
        before waiting for the real laser pulse."""
        if not self.spec.is_connected:
            self._send(AcquisitionMessage.ERROR, "Spectrometer not connected.")
            return
        self._set_state(self.STATE_TEST)
        self._send(AcquisitionMessage.STATUS, "Running test capture...")

    def stop(self):
        """Signal the worker thread to terminate."""
        self._stop_event.set()
        self._state_change_event.set()  # Wake up if sleeping

    def reset_shot_index(self):
        """Reset the shot counter (e.g., when sample name changes)."""
        self._shot_index = 0

    # ─── Thread Main Loop ─────────────────────────────────────────────

    def run(self):
        """Main thread loop — dispatches to state handlers."""
        logger.info("Acquisition worker started.")
        try:
            while not self._stop_event.is_set():
                current_state = self.state

                if current_state == self.STATE_LIVE:
                    self._run_live()
                elif current_state == self.STATE_ARMED:
                    self._run_armed()
                elif current_state == self.STATE_TEST:
                    self._run_test()
                else:
                    # IDLE — wait for a state change event
                    self._state_change_event.wait(timeout=0.5)
                    self._state_change_event.clear()
        except Exception as e:
            logger.error(f"Worker thread exception: {e}")
            self._send(AcquisitionMessage.ERROR, f"Worker error: {e}")
        finally:
            self._send(AcquisitionMessage.STOPPED, None)
            logger.info("Acquisition worker stopped.")

    def _run_live(self):
        """Continuous polling loop for live preview."""
        while self.state == self.STATE_LIVE and not self._stop_event.is_set():
            try:
                if self.averages > 1:
                    # Average multiple spectra
                    accumulated = None
                    for _ in range(self.averages):
                        intensities = self.spec.get_intensities(
                            correct_dark_counts=self.correct_dark_counts,
                            correct_nonlinearity=self.correct_nonlinearity
                        )
                        if accumulated is None:
                            accumulated = intensities.astype(np.float64)
                        else:
                            accumulated += intensities
                    intensities = accumulated / self.averages
                else:
                    intensities = self.spec.get_intensities(
                        correct_dark_counts=self.correct_dark_counts,
                        correct_nonlinearity=self.correct_nonlinearity
                    )

                wavelengths = self.spec.get_wavelengths()
                self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))

            except Exception as e:
                if self.state == self.STATE_LIVE:
                    self._send(AcquisitionMessage.ERROR, f"Live acquisition error: {e}")
                    self.go_idle()
                    return

            # Rate limit
            time.sleep(self.live_poll_interval)

    def _run_armed(self):
        """Continuously wait for hardware triggers, capture, auto-save, and re-arm.
        
        Loops until the user clicks Stop (state leaves ARMED) or the worker
        is shut down.  Each iteration:
            1. Set external trigger mode
            2. Submit a blocking read in a disposable thread
            3. Poll until capture completes (or user cancels)
            4. Process + save the captured spectrum
            5. Re-arm automatically
        
        The blocking intensities() call runs in a disposable thread so that
        if the user clicks Stop, we can detect the state change and avoid
        freezing the worker thread.  The blocking USB read will eventually
        return or timeout on its own once we flip the trigger mode back to
        normal in go_idle().
        """
        while self.state == self.STATE_ARMED and not self._stop_event.is_set():
            try:
                # Ensure we're in external trigger mode (re-arm)
                ext_mode = self.spec.capabilities.external_trigger_mode
                if ext_mode is not None and self.spec.current_trigger_mode != ext_mode:
                    self.spec.set_trigger_mode(ext_mode)

                self._send(AcquisitionMessage.STATUS, "Armed — waiting for laser trigger...")
                self._send(AcquisitionMessage.ARMED, None)

                # Launch the blocking read in a throwaway thread
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TriggerRead")
                future: Future = executor.submit(
                    self.spec.get_intensities,
                    correct_dark_counts=self.correct_dark_counts,
                    correct_nonlinearity=self.correct_nonlinearity,
                )

                # Poll the future so we can bail out if the user cancels
                cancelled = False
                while not future.done():
                    if self.state != self.STATE_ARMED or self._stop_event.is_set():
                        # User pressed Stop or we're shutting down.
                        # go_idle() already flipped trigger mode to normal which
                        # should unblock the USB read.  Just abandon the future.
                        self._send(AcquisitionMessage.STATUS, "Trigger cancelled.")
                        executor.shutdown(wait=False)
                        cancelled = True
                        break
                    time.sleep(0.1)  # Check 10× per second

                if cancelled:
                    break

                # If we get here the capture completed
                intensities = future.result()  # re-raises any exception from thread
                executor.shutdown(wait=False)

                wavelengths = self.spec.get_wavelengths()

                self._shot_index += 1
                self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
                self._send(AcquisitionMessage.CAPTURED, {
                    "wavelengths": wavelengths,
                    "intensities": intensities,
                    "shot_index": self._shot_index
                })
                self._send(AcquisitionMessage.STATUS,
                            f"Captured shot #{self._shot_index} — re-arming...")

                # Auto-save
                if self.auto_save_enabled:
                    self._auto_save(wavelengths, intensities)

            except Exception as e:
                error_msg = str(e)
                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    self._send(AcquisitionMessage.STATUS,
                               "Trigger timed out — re-arming...")
                    # Continue the loop to re-arm
                    continue
                else:
                    self._send(AcquisitionMessage.ERROR, f"Trigger capture error: {e}")
                    break  # Exit loop on non-timeout errors

        # ── Exited the loop — return to idle ───────────────────────────
        if self.spec.is_connected:
            try:
                normal_mode = self.spec.capabilities.normal_trigger_mode
                self.spec.set_trigger_mode(normal_mode)
            except Exception:
                pass
        self._set_state(self.STATE_IDLE)
        self._send(AcquisitionMessage.IDLE, None)

    def _run_test(self):
        """One-shot normal-mode read pushed through the full capture pipeline."""
        try:
            # Ensure normal mode for immediate read
            normal_mode = self.spec.capabilities.normal_trigger_mode
            if self.spec.current_trigger_mode != normal_mode:
                self.spec.set_trigger_mode(normal_mode)

            intensities = self.spec.get_intensities(
                correct_dark_counts=self.correct_dark_counts,
                correct_nonlinearity=self.correct_nonlinearity
            )
            wavelengths = self.spec.get_wavelengths()

            self._shot_index += 1
            self._send(AcquisitionMessage.SPECTRUM, (wavelengths, intensities))
            self._send(AcquisitionMessage.CAPTURED, {
                "wavelengths": wavelengths,
                "intensities": intensities,
                "shot_index": self._shot_index
            })
            self._send(AcquisitionMessage.STATUS,
                        f"Test capture #{self._shot_index} — pipeline OK")

            if self.auto_save_enabled:
                self._auto_save(wavelengths, intensities)

        except Exception as e:
            self._send(AcquisitionMessage.ERROR, f"Test trigger failed: {e}")
        finally:
            self._set_state(self.STATE_IDLE)
            self._send(AcquisitionMessage.IDLE, None)

    # ─── Auto-Save ─────────────────────────────────────────────────────

    def _auto_save(self, wavelengths: np.ndarray, intensities: np.ndarray):
        """Save the captured spectrum to a timestamped CSV file."""
        try:
            os.makedirs(self.save_directory, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.sample_name}_{timestamp}_{self._shot_index:03d}.csv"
            filepath = os.path.join(self.save_directory, filename)

            # Save as tab-delimited (consistent with LIBS data format)
            data = np.column_stack((wavelengths, intensities))
            header = "Wavelength\tIntensity"
            np.savetxt(filepath, data, delimiter='\t', header=header, comments='', fmt='%.6f')

            self._send(AcquisitionMessage.SAVE_COMPLETE, filepath)
            logger.info(f"Auto-saved: {filepath}")

        except Exception as e:
            self._send(AcquisitionMessage.ERROR, f"Auto-save failed: {e}")

    # ─── Messaging ─────────────────────────────────────────────────────

    def _send(self, msg_type: str, data):
        """Put a message on the queue for the GUI to consume."""
        self.message_queue.put((msg_type, data))
