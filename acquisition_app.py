# acquisition_app.py - Main application class for Acquisition Mode.
# Mirrors the structure of libs_app.py but provides spectrometer control instead of analysis.

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from ttkthemes import ThemedTk
import sv_ttk
import platform
import sys
import os
import numpy as np
import functools
import queue
import logging

logger = logging.getLogger(__name__)


class AcquisitionApp:
    """
    Acquisition Mode application.
    
    Provides:
        - Spectrometer connection via SpectrometerModule (any Ocean Optics model)
        - Live spectrum preview (continuous polling)
        - Hardware-triggered single-shot capture (external edge trigger)
        - Auto-save of captured spectra
        - Send captured data to Analysis Mode (in-memory hand-off)
    """

    def __init__(self):
        # DPI awareness
        if platform.system() == 'Windows':
            from ctypes import windll  # type: ignore
            windll.shcore.SetProcessDpiAwareness(1)

        # ─── Create Window ─────────────────────────────────────────────
        self.root = ThemedTk(theme="sun-valley")
        sv_ttk.set_theme("light")
        self.root.title("LIBS Acquisition Mode")
        self.root.geometry("1920x1080")
        self.root.minsize(width=1280, height=720)
        self.root.state("zoomed")

        try:
            self.root.iconbitmap('Icons/main_icon.ico')
        except Exception:
            pass

        # ─── State ─────────────────────────────────────────────────────
        self.spectrometer = None   # SpectrometerModule instance
        self.worker = None         # AcquisitionWorker instance

        # Current spectrum data (for save / send to analysis)
        self.current_wavelengths = None
        self.current_intensities = None
        self._highlight_line = None

        # Data to hand off to Analysis mode
        self._handoff_data = None

        # ─── Build UI ──────────────────────────────────────────────────
        # Graph area (offset from sidebar, same as analysis mode)
        graph_container = tk.Frame(self.root)
        graph_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=(300, 0))

        from acquisition_graph import create_acquisition_graph
        self.graph_frame, self.fig, self.ax, self.canvas, self.live_line = \
            create_acquisition_graph(graph_container)

        # Sidebar
        from acquisition_sidebar import create_acquisition_sidebar
        create_acquisition_sidebar(self)

        # ─── Window Close ──────────────────────────────────────────────
        self.root.protocol("WM_DELETE_WINDOW", functools.partial(self.on_closing))

    # ═══════════════════════════════════════════════════════════════════
    #  GUI Event Handlers (called by sidebar buttons)
    # ═══════════════════════════════════════════════════════════════════

    def on_connect(self):
        """Connect to the spectrometer."""
        from spectrometer import SpectrometerModule, SpectrometerError, NoDeviceError

        self.spectrometer = SpectrometerModule()
        try:
            status = self.spectrometer.connect()
        except NoDeviceError:
            # No hardware found — offer simulation mode
            result = messagebox.askyesno(
                "No Spectrometer Found",
                "No spectrometer hardware was detected.\n\n"
                "Would you like to use Simulation Mode?\n"
                "(Generates synthetic LIBS spectra for testing)"
            )
            if result:
                status = self.spectrometer.connect_simulated()
            else:
                self.spectrometer = None
                return
        except SpectrometerError as e:
            # seabreeze not installed — offer simulation directly
            result = messagebox.askyesno(
                "Connection Error",
                f"{e}\n\n"
                "Would you like to use Simulation Mode instead?"
            )
            if result:
                self.spectrometer = SpectrometerModule()
                status = self.spectrometer.connect_simulated()
            else:
                self.spectrometer = None
                return
        except Exception as e:
            messagebox.showerror("Error", f"Unexpected error:\n{e}")
            self.spectrometer = None
            return

        self.connection_status_var.set(status)
        self.status_message_var.set("Connected successfully.")

        # ── Configure UI for the connected device's capabilities ───
        caps = self.spectrometer.capabilities

        # Graph axis limits and placeholder
        from acquisition_graph import configure_graph_for_device
        configure_graph_for_device(self.ax, self.canvas, self.live_line, caps)

        # Integration time range hint
        int_min_ms = caps.integration_time_min_us / 1000.0
        int_max_ms = caps.integration_time_max_us / 1000.0
        self.int_range_var.set(f"Range: {int_min_ms:.2f}–{int_max_ms:.0f} ms")

        # Enable/disable correction checkboxes based on device support
        if hasattr(self, 'dark_check'):
            self.dark_check.config(
                state="normal" if caps.supports_dark_correction else "disabled"
            )
        if hasattr(self, 'nl_check'):
            self.nl_check.config(
                state="normal" if caps.supports_nonlinearity_correction else "disabled"
            )

        # Enable buttons
        self.connect_btn.config(state="disabled")
        self.disconnect_btn.config(state="normal")
        self.live_btn.config(state="normal")
        self.test_trigger_btn.config(state="normal")
        self.apply_int_btn.config(state="normal")

        # Arm Trigger button: only enable if the device supports external trigger
        if caps.has_external_trigger:
            self.arm_btn.config(state="normal")
        else:
            self.arm_btn.config(state="disabled")

        # Start the worker thread
        from acquisition_worker import AcquisitionWorker
        self.worker = AcquisitionWorker(self.spectrometer)
        self.worker.auto_save_enabled = self.auto_save_var.get()
        self.worker.save_directory = self.save_dir_var.get()
        self.worker.sample_name = self.sample_name_var.get()
        self.worker.start()

        # Start polling the message queue
        self._poll_queue()

    def on_disconnect(self):
        """Disconnect from the spectrometer."""
        if self.worker:
            self.worker.stop()
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        self.connection_status_var.set("Disconnected")
        self.worker_state_var.set("State: IDLE")
        self.status_message_var.set("Disconnected.")

        # Reset buttons
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.live_btn.config(state="disabled")
        self.arm_btn.config(state="disabled")
        self.test_trigger_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.apply_int_btn.config(state="disabled")

        # Reset device-specific UI hints
        if hasattr(self, 'int_range_var'):
            self.int_range_var.set("")
        if hasattr(self, 'dark_check'):
            self.dark_check.config(state="normal")
            self.correct_dark_var.set(False)
        if hasattr(self, 'nl_check'):
            self.nl_check.config(state="normal")
            self.correct_nl_var.set(False)

    def on_live_view(self):
        """Start live spectrum preview."""
        if self.worker:
            self.worker.start_live()
            self.live_btn.config(state="disabled")
            self.arm_btn.config(state="disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.worker_state_var.set("State: LIVE")

    def on_arm_trigger(self):
        """Arm the hardware trigger and wait for laser pulse."""
        if self.worker:
            self.worker.arm_trigger()
            self.live_btn.config(state="disabled")
            self.arm_btn.config(state="disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.worker_state_var.set("State: ARMED")

    def on_test_trigger(self):
        """Fire a test capture using normal mode to verify the full pipeline."""
        if self.worker:
            self.worker.test_trigger()
            self.live_btn.config(state="disabled")
            self.arm_btn.config(state="disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.worker_state_var.set("State: TEST")

    def on_stop(self):
        """Stop acquisition (live view or disarm trigger)."""
        if self.worker:
            self.worker.go_idle()
            self.live_btn.config(state="normal")
            self._update_arm_btn_state()
            self.test_trigger_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.worker_state_var.set("State: IDLE")

    def on_apply_integration(self):
        """Apply the integration time setting."""
        if not self.spectrometer or not self.spectrometer.is_connected:
            return

        try:
            ms = float(self.integration_var.get())
            us = int(ms * 1000)  # Convert ms to µs
            self.spectrometer.set_integration_time(us)
            self.status_message_var.set(f"Integration time: {ms} ms")
        except ValueError:
            messagebox.showwarning("Invalid Input", "Please enter a valid number for integration time.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_averages_changed(self):
        """Update the number of averages in the worker."""
        if self.worker:
            try:
                self.worker.averages = max(1, int(self.averages_var.get()))
            except ValueError:
                pass

    def on_corrections_changed(self):
        """Update dark count and nonlinearity correction flags in the worker."""
        if self.worker:
            self.worker.correct_dark_counts = self.correct_dark_var.get()
            self.worker.correct_nonlinearity = self.correct_nl_var.get()

    def on_auto_save_toggle(self):
        """Toggle auto-save on/off."""
        if self.worker:
            self.worker.auto_save_enabled = self.auto_save_var.get()

    def on_sample_name_changed(self):
        """Update the sample name in the worker and reset shot index."""
        if self.worker:
            self.worker.sample_name = self.sample_name_var.get()
            self.worker.reset_shot_index()
            self.shot_count_var.set("Shots: 0")

    def on_browse_save_dir(self):
        """Open a directory chooser for the auto-save location."""
        directory = filedialog.askdirectory(
            title="Select Save Directory",
            initialdir=self.save_dir_var.get()
        )
        if directory:
            self.save_dir_var.set(directory)
            if self.worker:
                self.worker.save_directory = directory

    def on_save_spectrum(self):
        """Manually save the current spectrum to a user-chosen file."""
        if self.current_wavelengths is None or self.current_intensities is None:
            messagebox.showinfo("No Data", "No spectrum data to save.")
            return

        filetypes = [("CSV files", "*.csv"), ("Text files", "*.txt"), ("All files", "*.*")]
        file_path = filedialog.asksaveasfilename(
            title="Save Spectrum",
            filetypes=filetypes,
            defaultextension=".csv"
        )
        if file_path:
            try:
                data = np.column_stack((self.current_wavelengths, self.current_intensities))
                np.savetxt(file_path, data, delimiter='\t',
                           header="Wavelength\tIntensity", comments='', fmt='%.6f')
                messagebox.showinfo("Saved", f"Spectrum saved to:\n{file_path}")
            except Exception as e:
                messagebox.showerror("Save Error", str(e))

    def on_send_to_analysis(self):
        """Store the current spectrum for hand-off to Analysis mode, then close."""
        if self.current_wavelengths is None or self.current_intensities is None:
            messagebox.showinfo("No Data", "No spectrum data to send.")
            return

        result = messagebox.askyesno(
            "Send to Analysis",
            "This will close Acquisition Mode and open Analysis Mode "
            "with the current spectrum loaded.\n\nContinue?"
        )
        if result:
            self._handoff_data = {
                "wavelengths": self.current_wavelengths.copy(),
                "intensities": self.current_intensities.copy()
            }
            self._cleanup_and_quit()

    # ═══════════════════════════════════════════════════════════════════
    #  Helpers
    # ═══════════════════════════════════════════════════════════════════

    def _update_arm_btn_state(self):
        """Enable the Arm Trigger button only if the device supports external trigger."""
        if self.spectrometer and self.spectrometer.is_connected:
            if self.spectrometer.capabilities.has_external_trigger:
                self.arm_btn.config(state="normal")
                return
        self.arm_btn.config(state="disabled")

    # ═══════════════════════════════════════════════════════════════════
    #  Message Queue Polling (thread-safe GUI updates)
    # ═══════════════════════════════════════════════════════════════════

    def _poll_queue(self):
        """Check the worker's message queue and process all pending messages."""
        if self.worker is None:
            return

        from acquisition_worker import AcquisitionMessage
        from acquisition_graph import update_spectrum_fast, highlight_captured_spectrum

        try:
            while True:
                msg_type, data = self.worker.message_queue.get_nowait()

                if msg_type == AcquisitionMessage.SPECTRUM:
                    wavelengths, intensities = data
                    self.current_wavelengths = wavelengths
                    self.current_intensities = intensities
                    update_spectrum_fast(self.ax, self.canvas, self.live_line,
                                         wavelengths, intensities)
                    # Enable save/send buttons now that we have data
                    self.save_spectrum_btn.config(state="normal")
                    self.send_to_analysis_btn.config(state="normal")

                elif msg_type == AcquisitionMessage.STATUS:
                    self.status_message_var.set(str(data))

                elif msg_type == AcquisitionMessage.ERROR:
                    self.status_message_var.set(f"Error: {data}")
                    logger.error(data)

                elif msg_type == AcquisitionMessage.ARMED:
                    self.worker_state_var.set("State: ARMED")

                elif msg_type == AcquisitionMessage.CAPTURED:
                    shot_idx = data["shot_index"]
                    self.shot_count_var.set(f"Shots: {shot_idx}")
                    # Visual feedback
                    self._highlight_line = highlight_captured_spectrum(
                        self.ax, self.canvas, data["wavelengths"],
                        data["intensities"], shot_idx
                    )
                    # Remove highlight after 2 seconds
                    self.root.after(2000, lambda: self._remove_highlight())
                    # Return buttons to idle state
                    self.live_btn.config(state="normal")
                    self._update_arm_btn_state()
                    self.test_trigger_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.worker_state_var.set("State: IDLE")

                elif msg_type == AcquisitionMessage.IDLE:
                    # Worker returned to idle — restore button state
                    self.live_btn.config(state="normal")
                    self._update_arm_btn_state()
                    self.test_trigger_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.worker_state_var.set("State: IDLE")

                elif msg_type == AcquisitionMessage.SAVE_COMPLETE:
                    self.status_message_var.set(f"Saved: {os.path.basename(data)}")

                elif msg_type == AcquisitionMessage.STOPPED:
                    self.worker_state_var.set("State: STOPPED")

        except queue.Empty:
            pass

        # Schedule next poll (50 ms)
        if self.worker:
            self.root.after(50, self._poll_queue)

    def _remove_highlight(self):
        """Remove the capture highlight line."""
        if self._highlight_line:
            from acquisition_graph import clear_highlight
            clear_highlight(self.ax, self.canvas, self._highlight_line)
            self._highlight_line = None

    # ═══════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ═══════════════════════════════════════════════════════════════════

    def run(self):
        """Start the Tkinter main loop."""
        self.root.mainloop()

    def get_handoff_data(self):
        """Return any spectrum data that should be passed to Analysis mode."""
        return self._handoff_data

    def _cleanup_and_quit(self):
        """Stop the worker, disconnect the spectrometer, and exit mainloop.
        Uses quit() instead of destroy() so that a new Tk root can be created
        afterwards (for the Analysis mode handoff)."""
        if self.worker:
            self.worker.stop()
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        self.root.quit()

    def _cleanup_and_close(self):
        """Stop the worker, disconnect the spectrometer, and destroy the window."""
        if self.worker:
            self.worker.stop()
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        self.root.destroy()

    def on_closing(self):
        """Handle window close event."""
        if self.worker and self.worker.state != "IDLE":
            result = messagebox.askyesno(
                "Confirm Exit",
                "Acquisition is in progress. Are you sure you want to exit?"
            )
            if not result:
                return

        self._handoff_data = None  # Don't hand off on close
        self._cleanup_and_close()
        sys.exit(0)
