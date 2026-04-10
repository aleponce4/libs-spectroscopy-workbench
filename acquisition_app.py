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
import threading
from PIL import Image, ImageDraw, ImageFont, ImageTk
from plate_autosave import (
    ORDER_COLUMN,
    ORDER_LABELS,
    ORDER_ROW,
    PLATE_FORMATS,
    PlateAutosaveConfig,
    PlateRunState,
)

# Import matplotlib BEFORE any Tk root is created, so the TkAgg backend
# initialises cleanly (avoids deadlock when a prior Tk root was destroyed).
import matplotlib
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


class AcquisitionApp:
    """
    Acquisition Mode application.
    
    Provides:
        - Spectrometer connection via SpectrometerModule (Ocean Optics) or
          ThorlabsCCSModule (CCS100/125/150/175/200) — user picks brand on connect
        - Live spectrum preview (continuous polling)
        - Hardware-triggered single-shot capture (external edge trigger)
        - Auto-save of captured spectra
        - Send captured data to Analysis Mode (in-memory hand-off)
    """

    def __init__(self, root=None):
        # DPI awareness
        if platform.system() == 'Windows':
            from ctypes import windll  # type: ignore
            windll.shcore.SetProcessDpiAwareness(1)

        # ─── Create Window ─────────────────────────────────────────────
        if root is not None:
            # Reuse the shared application root (no new Tk interpreter)
            self.root = root
        else:
            # Standalone launch
            self.root = ThemedTk(theme="sun-valley")
            sv_ttk.set_theme("light")

        self.root.title("LIBS Acquisition Mode")
        self.root.geometry("1920x1080")
        self.root.minsize(width=1280, height=720)
        self.root.state("zoomed")
        self.root.deiconify()  # Ensure visible (root may have been hidden)

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
        self.plate_autosave_config = None
        self.plate_progress = None
        self.plate_history = []
        self.current_plate_index = None

        # ─── Build UI ──────────────────────────────────────────────────
        # Graph area (offset from sidebar, same as analysis mode)
        self.graph_container = tk.Frame(self.root)
        self.graph_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=(300, 0))

        from acquisition_graph import create_acquisition_graph
        self.graph_frame, self.fig, self.ax, self.canvas, self.live_line = \
            create_acquisition_graph(self.graph_container)
        self.graph_frame.pack_forget()
        self.graph_container.grid_rowconfigure(0, weight=1)
        self.graph_container.grid_rowconfigure(1, weight=0)
        self.graph_container.grid_columnconfigure(0, weight=1)
        self.graph_frame.grid(row=0, column=0, sticky="nsew")

        self.plate_overview_frame = ttk.LabelFrame(
            self.graph_container,
            text="Plate Progress History",
            padding=(10, 6),
            height=380,
        )
        self.plate_overview_frame.pack_propagate(False)
        self.plate_history_canvas = tk.Canvas(
            self.plate_overview_frame,
            height=330,
            bg="#F7F9FC",
            highlightthickness=0,
        )
        self.plate_history_scrollbar = ttk.Scrollbar(
            self.plate_overview_frame,
            orient=tk.HORIZONTAL,
            command=self.plate_history_canvas.xview,
        )
        self.plate_history_canvas.configure(xscrollcommand=self.plate_history_scrollbar.set)
        self.plate_history_inner = ttk.Frame(self.plate_history_canvas)
        self.plate_history_window = self.plate_history_canvas.create_window(
            (0, 0),
            window=self.plate_history_inner,
            anchor="nw",
        )
        self.plate_history_inner.bind(
            "<Configure>",
            lambda event: self.plate_history_canvas.configure(
                scrollregion=self.plate_history_canvas.bbox("all")
            ),
        )
        self.plate_history_canvas.bind(
            "<Configure>",
            lambda event: self.plate_history_canvas.itemconfigure(
                self.plate_history_window,
                height=event.height,
            ),
        )
        self.plate_history_canvas.pack(fill=tk.X, expand=True)
        self.plate_history_scrollbar.pack(fill=tk.X)

        # Sidebar
        from acquisition_sidebar import create_acquisition_sidebar
        create_acquisition_sidebar(self)

        # ─── Window Close ──────────────────────────────────────────────
        self.root.protocol("WM_DELETE_WINDOW", functools.partial(self.on_closing))

    # ═══════════════════════════════════════════════════════════════════
    #  GUI Event Handlers (called by sidebar buttons)
    # ═══════════════════════════════════════════════════════════════════

    def on_connect(self):
        """Connect to the spectrometer — asks the user which brand first,
        or opens diagnostics on failure.

        The actual connection runs in a background thread so the GUI
        stays responsive during USB enumeration and device initialisation."""
        from spectrometer import (
            SpectrometerModule, ThorlabsCCSModule,
            SpectrometerError, NoDeviceError,
        )

        # ── Brand selection dialog ─────────────────────────────────────────
        brand = self._ask_brand()
        if brand is None:
            return  # user cancelled

        if brand == "simulation":
            self.spectrometer = SpectrometerModule()
            try:
                status = self.spectrometer.connect_simulated()
                self._finish_connection(status)
            except Exception as e:
                messagebox.showerror("Simulation Failed", str(e))
                self.spectrometer = None
            return

        if brand == "ocean_optics":
            self.spectrometer = SpectrometerModule()
        elif brand == "thorlabs":
            self.spectrometer = ThorlabsCCSModule()
        else:
            self.spectrometer = SpectrometerModule()

        # Disable the button and show progress while connecting
        self.connect_btn.config(state="disabled")
        self.status_message_var.set("Connecting… please wait.")
        self.root.update_idletasks()
        logger.info("on_connect: spawning background connect thread…")

        def _bg_connect():
            try:
                logger.info("_bg_connect: calling spectrometer.connect()…")
                status = self.spectrometer.connect()
                logger.info("_bg_connect: connect() returned OK")
                self.root.after(0, lambda s=status: self._finish_connection(s))
            except (NoDeviceError, SpectrometerError) as e:
                reason = str(e)
                logger.info(f"_bg_connect: connect() raised {type(e).__name__}: {reason}")
                self.root.after(0, lambda r=reason: self._on_connect_failed(r))
            except Exception as e:
                reason = f"Unexpected error: {e}"
                logger.info(f"_bg_connect: {reason}")
                self.root.after(0, lambda r=reason: self._on_connect_failed(r))

        threading.Thread(target=_bg_connect, daemon=True,
                         name="ConnectThread").start()

    def _on_connect_failed(self, reason: str):
        """Called on the main thread when background connect fails."""
        self.spectrometer = None
        self.connect_btn.config(state="normal")
        self.status_message_var.set("Connection failed.")
        result = self._open_diagnostic_dialog(auto_reason=reason)
        if result is None:
            return
        self._handle_diagnostic_result(result)

    def _open_diagnostic_dialog(self, auto_reason: str | None = None):
        """Show the diagnostic + device picker dialog.
        Returns the dialog result dict, or None if cancelled."""
        from diagnostic_dialog import DiagnosticDialog
        dlg = DiagnosticDialog(self.root, auto_reason=auto_reason)
        return dlg.result

    def _handle_diagnostic_result(self, result: dict):
        """Process the result from the diagnostic dialog.
        Connections are run in a background thread to keep the GUI responsive."""
        from spectrometer import (
            SpectrometerModule, ThorlabsCCSModule,
            SpectrometerError,
        )

        action = result.get("action")
        brand = result.get("brand", "ocean_optics")

        if action == "simulate":
            if brand == "thorlabs":
                self.spectrometer = ThorlabsCCSModule()
                status = self.spectrometer.connect_simulated("CCS175")
            else:
                self.spectrometer = SpectrometerModule()
                status = self.spectrometer.connect_simulated()
            self._finish_connection(status)

        elif action == "connect":
            device_index = result.get("device_index", 0)
            if brand == "thorlabs":
                self.spectrometer = ThorlabsCCSModule()
            else:
                self.spectrometer = SpectrometerModule()

            self.connect_btn.config(state="disabled")
            self.status_message_var.set("Connecting… please wait.")
            self.root.update_idletasks()

            def _bg():
                try:
                    status = self.spectrometer.connect(device_index=device_index)
                    self.root.after(0, lambda s=status: self._finish_connection(s))
                except Exception as e:
                    msg = f"Could not connect to device {device_index}:\n{e}"
                    def _fail(m=msg):
                        messagebox.showerror("Connection Failed", m)
                        self.spectrometer = None
                        self.connect_btn.config(state="normal")
                    self.root.after(0, _fail)

            threading.Thread(target=_bg, daemon=True,
                             name="DiagConnectThread").start()

        elif action == "connect_resource":
            resource = result.get("resource", "")
            self.spectrometer = ThorlabsCCSModule()

            self.connect_btn.config(state="disabled")
            self.status_message_var.set("Connecting… please wait.")
            self.root.update_idletasks()

            def _bg_visa():
                try:
                    status = self.spectrometer.connect_with_resource(resource)
                    self.root.after(0, lambda s=status: self._finish_connection(s))
                except Exception as e:
                    msg = f"Could not connect to VISA resource:\n{resource}\n\n{e}"
                    def _fail(m=msg):
                        messagebox.showerror("Connection Failed", m)
                        self.spectrometer = None
                        self.connect_btn.config(state="normal")
                    self.root.after(0, _fail)

            threading.Thread(target=_bg_visa, daemon=True,
                             name="VISAConnectThread").start()

    def _finish_connection(self, status: str):
        """Common post-connection setup (UI configuration, worker start)."""
        caps = self.spectrometer.capabilities
        model = caps.model or "Spectrometer"
        if len(model) > 32:
            model = f"{model[:29]}..."
        simulated = " [SIM]" if "SIMULATED" in status.upper() or caps.brand == "simulated" else ""
        self.connection_status_var.set(f"Connected: {model}{simulated}")
        self.status_message_var.set("Connected successfully.")

        # ── Configure UI for the connected device's capabilities ───
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
        if self.plate_mode_var.get() and self.plate_autosave_config is not None:
            self.worker.set_plate_autosave_config(self.plate_autosave_config)
        self.worker.start()

        # Start polling the message queue
        self._poll_queue()

    def on_diagnose(self):
        """Open the diagnostic dialog on demand (sidebar button)."""
        result = self._open_diagnostic_dialog()
        if result is not None:
            # If already connected, disconnect first
            if self.spectrometer and self.spectrometer.is_connected:
                self.on_disconnect()
            self._handle_diagnostic_result(result)

    def on_disconnect(self):
        """Disconnect from the spectrometer."""
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
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
        if hasattr(self, 'discard_plate_shot_btn'):
            self.discard_plate_shot_btn.config(state="disabled")
        if hasattr(self, 'plate_progress_var'):
            self.plate_mode_var.set(False)
            self.configure_plate_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._hide_plate_overview()

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
        """Arm the hardware trigger and wait for laser pulse.
        If loop-arm is enabled, the worker will automatically re-arm
        after each capture until the user clicks Stop."""
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
        """Stop acquisition (live view or disarm trigger / loop)."""
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
        if self.plate_mode_var.get() and not self.auto_save_var.get():
            messagebox.showinfo(
                "Plate Mode Uses Auto-Save",
                "High-throughput plate mode needs auto-save so each trigger can advance the plate map.",
            )
            self.auto_save_var.set(True)
        if self.worker:
            self.worker.auto_save_enabled = self.auto_save_var.get()

    def on_plate_mode_toggle(self):
        """Enable or disable high-throughput plate autosave."""
        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing high-throughput plate mode.",
            )
            self.plate_mode_var.set(self.plate_progress is not None)
            return

        if self.plate_mode_var.get():
            self.auto_save_var.set(True)
            self.on_auto_save_toggle()
            self.configure_plate_btn.config(state="normal")
            if not self.on_configure_plate():
                self.plate_mode_var.set(False)
                self.configure_plate_btn.config(state="disabled")
                self.plate_progress_var.set("")
                self.plate_history = []
                self.current_plate_index = None
                self._hide_plate_overview()
        else:
            if self.worker:
                self.worker.disable_plate_autosave()
            self.configure_plate_btn.config(state="disabled")
            self.discard_plate_shot_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._hide_plate_overview()

    def on_configure_plate(self):
        """Open the high-throughput plate settings dialog."""
        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing plate settings.",
            )
            return False

        config = self._ask_plate_settings()
        if config is None:
            return False
        self._apply_plate_config(config)
        return True

    def on_discard_last_plate_shot(self):
        """Discard the latest saved high-throughput plate shot."""
        if self.worker:
            self.worker.discard_last_plate_shot()

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

    def _is_acquisition_busy(self):
        return bool(self.worker and self.worker.state != "IDLE")

    def _apply_plate_config(self, config):
        if not isinstance(config, PlateAutosaveConfig):
            config = PlateAutosaveConfig.from_mapping(config)

        self.plate_autosave_config = config
        self.plate_mode_var.set(True)
        self.configure_plate_btn.config(state="normal")
        self.auto_save_var.set(True)
        self.on_auto_save_toggle()

        state = PlateRunState(config)
        payload = state.progress_payload()
        self._start_plate_history_card(payload)
        self._update_plate_progress(payload)
        if self.worker:
            self.worker.set_plate_autosave_config(config)

    def _ask_plate_settings(self):
        """Show the modal high-throughput plate settings dialog."""
        current = self.plate_autosave_config or PlateAutosaveConfig()
        result = {"config": None}

        dlg = tk.Toplevel(self.root)
        dlg.title("High-Throughput Plate Settings")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        dlg.update_idletasks()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        dw, dh = 720, 520
        dlg.geometry(f"{dw}x{dh}+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

        main = ttk.Frame(dlg, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            main,
            text="High-throughput plate autosave",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        form = ttk.LabelFrame(main, text="Run Settings", padding=10)
        form.grid(row=1, column=0, sticky="nsw", padx=(0, 12))

        plate_type_var = tk.StringVar(value=str(current.plate_type))
        plate_name_var = tk.StringVar(value=current.plate_name)
        shots_var = tk.StringVar(value=str(current.shots_per_well))
        order_var = tk.StringVar(value=current.order_mode)

        ttk.Label(form, text="Plate type:").grid(row=0, column=0, sticky="w", pady=4)
        plate_combo = ttk.Combobox(
            form,
            textvariable=plate_type_var,
            values=[str(value) for value in PLATE_FORMATS],
            width=12,
            state="readonly",
        )
        plate_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Plate name:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=plate_name_var, width=20).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Shots per well:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(form, from_=1, to=99, textvariable=shots_var, width=8).grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(form, text="Order:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Radiobutton(form, text=ORDER_LABELS[ORDER_ROW], value=ORDER_ROW, variable=order_var).grid(
            row=3, column=1, sticky="w", pady=(4, 1)
        )
        ttk.Radiobutton(form, text=ORDER_LABELS[ORDER_COLUMN], value=ORDER_COLUMN, variable=order_var).grid(
            row=4, column=1, sticky="w", pady=(1, 4)
        )

        ttk.Label(
            form,
            text="Files are saved into a subfolder named after the plate.",
            style="Status.TLabel",
            wraplength=210,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

        preview_frame = ttk.LabelFrame(main, text="Plate Preview", padding=10)
        preview_frame.grid(row=1, column=1, sticky="nsew")
        preview_canvas = tk.Canvas(
            preview_frame,
            width=420,
            height=320,
            bg="#F7F9FC",
            highlightthickness=1,
            highlightbackground="#C8D0DA",
        )
        preview_canvas.pack(fill=tk.BOTH, expand=True)

        button_bar = ttk.Frame(main)
        button_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        def _current_dialog_config():
            return PlateAutosaveConfig.from_mapping({
                "plate_type": plate_type_var.get(),
                "plate_name": plate_name_var.get(),
                "shots_per_well": shots_var.get(),
                "order_mode": order_var.get(),
            })

        def _redraw_preview(*_):
            try:
                preview_state = PlateRunState(_current_dialog_config())
                self._draw_plate_payload(preview_canvas, preview_state.progress_payload(), preview=True)
            except Exception:
                preview_canvas.delete("all")
                preview_canvas.create_text(
                    210,
                    160,
                    text="Enter a valid shot count.",
                    fill="#A33",
                    font=("Segoe UI", 10, "bold"),
                )

        def _apply():
            try:
                config = _current_dialog_config()
            except Exception:
                messagebox.showwarning("Invalid Plate Settings", "Please enter a valid shot count.", parent=dlg)
                return
            result["config"] = config
            dlg.destroy()

        for var in (plate_type_var, plate_name_var, shots_var, order_var):
            var.trace_add("write", _redraw_preview)

        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Use Plate Settings", width=18, command=_apply).pack(side=tk.RIGHT)

        _redraw_preview()
        dlg.wait_window()
        return result["config"]

    def _update_plate_progress(self, payload):
        self.plate_progress = payload
        if not payload:
            return

        self._update_plate_history_card(payload)
        if payload["current_well"] is None:
            next_text = "complete"
        else:
            next_text = f"next {payload['current_well']}"

        self.plate_progress_var.set(
            f"{payload['plate_name']} ({payload['plate_type']}-well): "
            f"{payload['complete_wells']}/{payload['total_wells']} wells, {next_text}"
        )
        self._show_plate_overview()
        self._redraw_plate_history()
        self.discard_plate_shot_btn.config(state="normal" if payload.get("can_discard") else "disabled")

    def _start_plate_history_card(self, payload):
        if self.current_plate_index is None:
            self.plate_history = [dict(payload)]
            self.current_plate_index = 0
            return

        current = self.plate_history[self.current_plate_index]
        if current.get("complete"):
            if self.current_plate_index < len(self.plate_history) - 1:
                self.current_plate_index += 1
                self.plate_history[self.current_plate_index] = dict(payload)
            else:
                self.plate_history.append(dict(payload))
                self.current_plate_index = len(self.plate_history) - 1
        else:
            self.plate_history[self.current_plate_index] = dict(payload)

    def _update_plate_history_card(self, payload):
        if self.current_plate_index is None:
            self._start_plate_history_card(payload)
            return
        self.plate_history[self.current_plate_index] = dict(payload)

    def _append_next_plate_placeholder(self, payload):
        if self.current_plate_index is None or not payload.get("complete"):
            return
        if self.current_plate_index < len(self.plate_history) - 1:
            return

        config = PlateAutosaveConfig.from_mapping({
            "plate_type": payload["plate_type"],
            "plate_name": payload["plate_name"],
            "shots_per_well": payload["shots_per_well"],
            "order_mode": payload["order_mode"],
        })
        next_payload = PlateRunState(config).progress_payload()
        self.plate_history.append(next_payload)

    def _show_plate_overview(self):
        if not self.plate_overview_frame.winfo_ismapped():
            self.plate_overview_frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
            self.root.update_idletasks()
            self.canvas.draw_idle()

    def _hide_plate_overview(self):
        if hasattr(self, "plate_overview_frame"):
            self.plate_overview_frame.grid_forget()

    def _redraw_plate_history(self, focus_index=None):
        for child in self.plate_history_inner.winfo_children():
            child.destroy()

        for index, payload in enumerate(self.plate_history):
            title = f"{payload['plate_name']} - Plate {index + 1}"
            if index == self.current_plate_index and not payload.get("complete"):
                title += " (current)"
            elif payload.get("complete"):
                title += " (done)"
            elif self.current_plate_index is not None and index > self.current_plate_index:
                title += " (next)"

            card = ttk.LabelFrame(self.plate_history_inner, text=title, padding=(6, 4))
            card.grid(row=0, column=index, sticky="n", padx=(0, 10), pady=2)
            plate_canvas = tk.Canvas(
                card,
                width=430,
                height=290,
                bg="#F7F9FC",
                highlightthickness=1,
                highlightbackground="#C8D0DA",
            )
            plate_canvas.pack()
            self._draw_plate_payload(plate_canvas, payload, preview=False)

        self.plate_history_inner.update_idletasks()
        self.plate_history_canvas.configure(scrollregion=self.plate_history_canvas.bbox("all"))

        if focus_index is None:
            focus_index = self.current_plate_index
        if focus_index is None or not self.plate_history_inner.winfo_children():
            return

        focus_index = max(0, min(focus_index, len(self.plate_history_inner.winfo_children()) - 1))
        target = self.plate_history_inner.winfo_children()[focus_index]
        scroll_region = self.plate_history_canvas.bbox("all")
        if not scroll_region:
            return

        content_width = scroll_region[2] - scroll_region[0]
        visible_width = self.plate_history_canvas.winfo_width()
        if content_width <= visible_width:
            self.plate_history_canvas.xview_moveto(0)
            return
        max_scroll = max(1, content_width - visible_width)
        self.plate_history_canvas.xview_moveto(max(0, min(target.winfo_x() / max_scroll, 1)))

    def _draw_plate_payload(self, canvas, payload, preview=False):
        canvas.delete("all")

        width = max(canvas.winfo_width(), int(canvas.cget("width") or 1))
        height = max(canvas.winfo_height(), int(canvas.cget("height") or 1))
        scale = 3
        rows = payload["rows"]
        columns = payload["columns"]
        shots_per_well = payload["shots_per_well"]
        shots_by_well = payload["shots_by_well"]
        current_well = payload["current_well"]

        image = Image.new("RGB", (width * scale, height * scale), "#F7F9FC")
        draw = ImageDraw.Draw(image)

        def px(value):
            return int(round(value * scale))

        def xy(x, y):
            return (px(x), px(y))

        def font(size, bold=False):
            font_name = "segoeuib.ttf" if bold else "segoeui.ttf"
            try:
                return ImageFont.truetype(font_name, max(6, px(size)))
            except OSError:
                return ImageFont.load_default()

        def draw_arrow_line(start, end, color):
            draw.line((xy(*start), xy(*end)), fill=color, width=max(1, px(2)))
            sx, sy = start
            ex, ey = end
            if abs(ex - sx) >= abs(ey - sy):
                direction = 1 if ex >= sx else -1
                arrow = [
                    xy(ex, ey),
                    xy(ex - direction * 7, ey - 4),
                    xy(ex - direction * 7, ey + 4),
                ]
            else:
                direction = 1 if ey >= sy else -1
                arrow = [
                    xy(ex, ey),
                    xy(ex - 4, ey - direction * 7),
                    xy(ex + 4, ey - direction * 7),
                ]
            draw.polygon(arrow, fill=color)

        title = f"{payload['plate_type']}-well - {payload['order_label']}"
        draw.text(xy(8, 10), title, anchor="lt", fill="#1E2B36", font=font(9, bold=True))
        if payload["order_mode"] == ORDER_ROW:
            arrow_text = "Move left to right, then next row"
        else:
            arrow_text = "Move top to bottom, then next column"
        draw.text(xy(8, 29), arrow_text, anchor="lt", fill="#546270", font=font(8))

        left_bound = 54 if preview else 48
        top_bound = 76 if preview else 72
        right_bound = width - 16
        bottom_bound = height - 16
        available_w = max(10, right_bound - left_bound)
        available_h = max(10, bottom_bound - top_bound)
        plate_ratio = columns / rows
        if available_w / available_h > plate_ratio:
            grid_h = available_h
            grid_w = grid_h * plate_ratio
        else:
            grid_w = available_w
            grid_h = grid_w / plate_ratio
        left = left_bound + (available_w - grid_w) / 2
        top = top_bound + (available_h - grid_h) / 2
        right = left + grid_w
        bottom = top + grid_h
        cell_w = (right - left) / columns
        cell_h = (bottom - top) / rows
        min_cell = min(cell_w, cell_h)
        radius = max(2, min_cell * 0.36)
        count_font_size = 8 if min_cell >= 16 else 6
        show_counts = min_cell >= 9
        show_edge_labels = min_cell >= 8
        edge_font = font(8 if min_cell >= 13 else 6, bold=True)
        count_font = font(count_font_size, bold=True)

        if payload["order_mode"] == ORDER_ROW:
            draw_arrow_line((left + cell_w * 0.2, top - 27), (right - cell_w * 0.2, top - 27), "#1D6FB8")
        else:
            draw_arrow_line((left - 28, top + cell_h * 0.2), (left - 28, bottom - cell_h * 0.2), "#1D6FB8")

        if show_edge_labels:
            for column in range(1, columns + 1):
                cx = left + (column - 0.5) * cell_w
                draw.text(xy(cx, top - 13), str(column), anchor="mm", fill="#455462", font=edge_font)
            for row in range(rows):
                cy = top + (row + 0.5) * cell_h
                draw.text(xy(left - 13, cy), chr(65 + row), anchor="mm", fill="#455462", font=edge_font)

        for row in range(rows):
            for column in range(1, columns + 1):
                well = f"{chr(65 + row)}{column}"
                count = shots_by_well.get(well, 0)
                cx = left + (column - 0.5) * cell_w
                cy = top + (row + 0.5) * cell_h
                fill = "#DDE5EE"
                outline = "#9BA8B4"
                width_px = 1
                if count >= shots_per_well:
                    fill = "#55B97D"
                    outline = "#2F7E50"
                elif count > 0:
                    fill = "#F3C760"
                    outline = "#A87A16"
                if well == current_well:
                    outline = "#D33232"
                    width_px = 2

                draw.ellipse(
                    (
                        px(cx - radius),
                        px(cy - radius),
                        px(cx + radius),
                        px(cy + radius),
                    ),
                    fill=fill,
                    outline=outline,
                    width=max(1, px(width_px)),
                )
                if show_counts and count > 0:
                    draw.text(xy(cx, cy), str(count), anchor="mm", fill="#14212B", font=count_font)

        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        photo = ImageTk.PhotoImage(image.resize((width, height), resample))
        canvas.create_image(0, 0, image=photo, anchor=tk.NW)
        canvas._plate_image = photo

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

                    # The worker automatically re-arms after each capture.
                    # While it stays in ARMED state, keep Stop enabled.
                    if self.worker and self.worker.state == "ARMED":
                        self.worker_state_var.set(f"State: ARMED (shot {shot_idx})")
                    else:
                        # Worker returned to idle (error or single-shot test)
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

                elif msg_type == AcquisitionMessage.PLATE_PROGRESS:
                    self._update_plate_progress(data)

                elif msg_type == AcquisitionMessage.PLATE_DISCARDED:
                    self._update_plate_progress(data)
                    discarded = data.get("discarded")
                    if discarded:
                        self.status_message_var.set(f"Discarded: {os.path.basename(discarded)}")

                elif msg_type == AcquisitionMessage.PLATE_COMPLETE:
                    self._update_plate_progress(data)
                    self._append_next_plate_placeholder(data)
                    self._redraw_plate_history(focus_index=len(self.plate_history) - 1)
                    self.status_message_var.set("Plate complete.")

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
    #  Brand selection dialog
    # ═══════════════════════════════════════════════════════════════════

    def _ask_brand(self) -> str | None:
        """
        Show a small dialog asking the user to pick a spectrometer brand.
        
        Returns ``"ocean_optics"``, ``"thorlabs"``, ``"simulation"``,
        or ``None`` if cancelled.
        """
        result = {"value": None}

        dlg = tk.Toplevel(self.root)
        dlg.title("Select Spectrometer Connection")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        # Centre on parent
        dlg.update_idletasks()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        dw, dh = 560, 190
        dlg.geometry(f"{dw}x{dh}+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

        ttk.Label(dlg, text="Choose a spectrometer connection",
                  font=("Segoe UI", 11, "bold")).pack(pady=(18, 10))

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=4)

        def _pick(brand):
            result["value"] = brand
            dlg.destroy()

        ttk.Button(btn_frame, text="Ocean Optics", width=16,
                   command=lambda: _pick("ocean_optics")).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text="Thorlabs CCS", width=16,
                   command=lambda: _pick("thorlabs")).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text="Simulation Mode", width=18,
                   command=lambda: _pick("simulation")).pack(side=tk.LEFT, padx=8)

        ttk.Button(dlg, text="Cancel", width=10,
                   command=dlg.destroy).pack(pady=(16, 0))

        dlg.wait_window()
        return result["value"]

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
        Uses quit() so the shared root stays alive for Analysis mode handoff."""
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        # Remove all acquisition widgets so the root can be reused
        for widget in self.root.winfo_children():
            widget.destroy()

        self.root.quit()

    def _cleanup_and_close(self):
        """Stop the worker, disconnect the spectrometer, and exit.
        Used when the user closes the window without handoff."""
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
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
