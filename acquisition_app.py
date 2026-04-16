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
import time
from PIL import Image, ImageDraw, ImageFont, ImageTk
from acquisition_sidebar import ACQUISITION_SIDEBAR_WIDTH, create_acquisition_sidebar
from plate_autosave import (
    ORDER_COLUMN,
    discover_resumable_plate_runs,
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
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        min_width = min(max(1000, ACQUISITION_SIDEBAR_WIDTH + 620), screen_w)
        min_height = min(640, screen_h)
        self.root.minsize(width=min_width, height=min_height)

        window_w = max(min(1680, screen_w - 80), min_width)
        window_h = max(min(980, screen_h - 80), min_height)
        offset_x = max((screen_w - window_w) // 2, 0)
        offset_y = max((screen_h - window_h) // 2, 0)
        self.root.geometry(f"{window_w}x{window_h}+{offset_x}+{offset_y}")
        if platform.system() == "Windows" and screen_w >= 1600 and screen_h >= 900:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass
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
        self._highlight_after_id = None

        # Data to hand off to Analysis mode
        self._handoff_data = None
        self.plate_autosave_config = None
        self.plate_progress = None
        self.plate_history = []
        self.current_plate_index = None
        self._plate_completion_prompt_pending = False
        self._pending_plate_run_state = None
        self._queue_poll_after_id = None
        self.timing_samples = []
        self.latest_timing_sample = None
        self._pending_spectrum = None
        self._pending_draw_after_id = None
        self._last_plot_draw_at = 0.0
        self.active_queue_poll_ms = 15
        self.idle_queue_poll_ms = 75
        self.live_redraw_interval_ms = 33
        # ─── Build UI ──────────────────────────────────────────────────
        # Graph area (offset from sidebar, same as analysis mode)
        self.graph_container = tk.Frame(self.root)
        self.graph_container.pack(
            side=tk.TOP,
            fill=tk.BOTH,
            expand=True,
            padx=(ACQUISITION_SIDEBAR_WIDTH + 20, 0),
        )

        from acquisition_graph import create_acquisition_graph
        self.graph_frame, self.fig, self.ax, self.canvas, self.live_line = \
            create_acquisition_graph(self.graph_container)
        self._highlight_line = self.live_line
        self.graph_frame.pack_forget()
        self.graph_container.grid_rowconfigure(0, weight=1)
        self.graph_container.grid_rowconfigure(1, weight=0)
        self.graph_container.grid_columnconfigure(0, weight=1)
        self.graph_frame.grid(row=0, column=0, sticky="nsew")

        self.plate_overview_frame = ttk.Frame(
            self.graph_container,
            padding=(10, 6),
            height=440,
        )
        self.plate_overview_frame.pack_propagate(False)
        self.plate_history_canvas = tk.Canvas(
            self.plate_overview_frame,
            height=390,
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
        self.plate_history_canvas.pack(fill=tk.BOTH, expand=True)
        self.plate_history_scrollbar.pack(fill=tk.X)

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
        self.timing_samples = []
        self.latest_timing_sample = None
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

        # Keep the trigger action obvious once the instrument is ready.
        self._update_arm_btn_state()

        # Start the worker thread
        from acquisition_worker import AcquisitionWorker
        self.worker = AcquisitionWorker(self.spectrometer)
        self.worker.auto_save_enabled = self.auto_save_var.get()
        self.worker.save_directory = self.save_dir_var.get()
        self.worker.sample_name = self.sample_name_var.get()
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
        self._cancel_queue_poll()
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        self.connection_status_var.set("Disconnected")
        self._set_worker_state("State: IDLE")
        self.status_message_var.set("Disconnected.")

        # Reset buttons
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.live_btn.config(state="disabled")
        self._set_arm_button_visual("disabled")
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
        if hasattr(self, 'repair_plate_wells_btn'):
            self.repair_plate_wells_btn.config(state="disabled")
        if hasattr(self, 'finish_plate_btn'):
            self.finish_plate_btn.config(state="disabled")
        if hasattr(self, 'plate_progress_var'):
            self.plate_mode_var.set(False)
            self.configure_plate_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._plate_completion_prompt_pending = False
            self._pending_plate_run_state = None
            self._hide_plate_overview()
        self.timing_samples = []
        self.latest_timing_sample = None

    def on_live_view(self):
        """Start live spectrum preview."""
        if self.worker:
            self.worker.start_live()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: LIVE")

    def on_arm_trigger(self):
        """Arm the hardware trigger and wait for laser pulse.
        If loop-arm is enabled, the worker will automatically re-arm
        after each capture until the user clicks Stop."""
        if self._plate_requires_reconfigure():
            messagebox.showinfo(
                "Configure Next Plate",
                "Finish configuring the next plate before starting another capture.",
            )
            return
        if self.worker:
            if self.worker.state == "ARMED":
                return
            self.worker.arm_trigger()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("armed")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: ARMED")

    def on_test_trigger(self):
        """Fire a test capture using normal mode to verify the full pipeline."""
        if self._plate_requires_reconfigure():
            messagebox.showinfo(
                "Configure Next Plate",
                "Finish configuring the next plate before running another test capture.",
            )
            return
        if self.worker:
            self.worker.test_trigger()
            self.live_btn.config(state="disabled")
            self._set_arm_button_visual("disabled")
            self.test_trigger_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self._set_worker_state("State: TEST")

    def on_stop(self):
        """Stop acquisition (live view or disarm trigger / loop)."""
        if self.worker:
            self.worker.go_idle()
            self._remove_highlight()
            self.live_btn.config(state="normal")
            self._update_arm_btn_state()
            self.test_trigger_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self._set_worker_state("State: IDLE")

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
            self.repair_plate_wells_btn.config(state="disabled")
            self.finish_plate_btn.config(state="disabled")
            self.plate_progress_var.set("")
            self.plate_progress = None
            self.plate_history = []
            self.current_plate_index = None
            self._plate_completion_prompt_pending = False
            self._pending_plate_run_state = None
            self._hide_plate_overview()

    def on_configure_plate(self):
        """Open the high-throughput plate settings dialog."""
        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before changing plate settings.",
            )
            return False

        selection = self._ask_plate_settings()
        if selection is None:
            return False
        if isinstance(selection, PlateRunState):
            self._apply_resumed_plate_state(selection)
        else:
            self._apply_plate_config(selection)
        return True

    def on_discard_last_plate_shot(self):
        """Discard the latest saved high-throughput plate shot."""
        if self.worker:
            self.worker.discard_last_plate_shot()

    def on_repair_plate_wells(self):
        """Pause the current plate run, select wells, and queue a repair pass."""
        if (
            not self.worker
            or not self.plate_progress
            or self._plate_payload_finished(self.plate_progress)
            or self.plate_progress.get("repair_active")
        ):
            return

        resume_state = self._pause_plate_worker_for_modal()
        selected_wells = None
        try:
            selected_wells = self._ask_plate_repair_wells()
        finally:
            if not selected_wells:
                self._resume_plate_worker_mode(resume_state)

        if not selected_wells:
            return

        self.worker.start_plate_repair(selected_wells)
        self._resume_plate_worker_mode(resume_state)

    def on_finish_plate_early(self):
        """Close the current plate early and prompt for the next one."""
        if not self.plate_progress or self._plate_payload_finished(self.plate_progress):
            return
        if self.plate_progress.get("repair_active"):
            messagebox.showinfo(
                "Repair In Progress",
                "Finish the current repair pass before closing the plate early.",
                parent=self.root,
            )
            return

        if self._is_acquisition_busy():
            messagebox.showinfo(
                "Acquisition Running",
                "Stop acquisition before finishing the current plate early.",
            )
            return

        if not messagebox.askyesno(
            "Finish Plate Early",
            "This will mark the remaining wells as skipped and let you configure the next plate.\n\nContinue?",
            parent=self.root,
        ):
            return

        payload = dict(self.plate_progress)
        payload["closed_early"] = True
        payload["current_well"] = None
        payload["can_discard"] = False
        self._update_plate_progress(payload)
        self.status_message_var.set("Plate closed early.")
        if self.worker:
            self.worker.close_plate_run_early()
        self._pending_plate_run_state = None
        self._prompt_for_next_plate()

    def _pause_plate_worker_for_modal(self):
        """Pause live/armed acquisition before opening a modal plate action."""
        if not self.worker:
            return None

        previous_state = self.worker.state
        if previous_state in ("LIVE", "ARMED", "TEST"):
            self.on_stop()
            return previous_state
        return None

    def _resume_plate_worker_mode(self, previous_state):
        """Restore the worker mode that was active before a modal plate action."""
        if previous_state == "ARMED":
            self.on_arm_trigger()
        elif previous_state == "LIVE":
            self.on_live_view()

    def _format_well_list(self, wells, max_items=8):
        ordered = [well for well in self.plate_autosave_config.ordered_wells if well in set(wells)] if self.plate_autosave_config else list(wells)
        if len(ordered) <= max_items:
            return ", ".join(ordered)
        return f"{', '.join(ordered[:max_items])}, +{len(ordered) - max_items} more"

    def _ask_plate_repair_wells(self):
        """Let the user click wells to repair on the current plate map."""
        if not self.plate_progress:
            return None

        repairable_wells = {
            well for well, count in self.plate_progress["shots_by_well"].items()
            if count > 0
        }
        if not repairable_wells:
            messagebox.showinfo(
                "No Saved Wells",
                "There are no saved wells available to repair yet.",
                parent=self.root,
            )
            return None

        result = {"selection": None}
        selected_wells: set[str] = set()

        dlg = tk.Toplevel(self.root)
        dlg.title("Repair Plate Wells")
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.transient(self.root)

        dlg.update_idletasks()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        dw, dh = 860, 660
        dlg.geometry(f"{dw}x{dh}+{px + max((pw - dw) // 2, 0)}+{py + max((ph - dh) // 2, 0)}")
        dlg.minsize(760, 580)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="Select wells to repair",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        ttk.Label(
            outer,
            text="Click any saved well to re-shoot it. The current files for those wells will move to Discarded, then the plate run will temporarily revisit them before resuming the normal order.",
            style="Status.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        preview = ttk.LabelFrame(outer, text="Plate Repair Map", padding=10)
        preview.grid(row=2, column=0, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            preview,
            width=700,
            height=430,
            bg="#F7F9FC",
            highlightthickness=1,
            highlightbackground="#C8D0DA",
        )
        canvas.grid(row=0, column=0, sticky="nsew")

        selected_var = tk.StringVar(value="Selected wells: none")
        ttk.Label(
            outer,
            textvariable=selected_var,
            style="StatusValue.TLabel",
            wraplength=760,
        ).grid(row=3, column=0, sticky="ew", pady=(10, 0))

        def _refresh_summary():
            if selected_wells:
                selected_var.set(f"Selected wells: {self._format_well_list(selected_wells)}")
            else:
                selected_var.set("Selected wells: none")

        def _redraw():
            payload = dict(self.plate_progress)
            self._draw_plate_payload(
                canvas,
                payload,
                preview=False,
                selected_wells=selected_wells,
                disabled_wells=set(payload["shots_by_well"]) - repairable_wells,
            )

        def _on_click(event):
            well = self._well_from_canvas_point(
                self.plate_progress,
                event.x,
                event.y,
                preview=False,
                canvas_width=canvas.winfo_width(),
                canvas_height=canvas.winfo_height(),
            )
            if not well or well not in repairable_wells:
                return
            if well in selected_wells:
                selected_wells.remove(well)
            else:
                selected_wells.add(well)
            _refresh_summary()
            _redraw()

        def _confirm():
            if not selected_wells:
                messagebox.showinfo(
                    "Select Wells",
                    "Choose at least one saved well to repair.",
                    parent=dlg,
                )
                return
            ordered = [
                well for well in self.plate_autosave_config.ordered_wells
                if well in selected_wells
            ]
            result["selection"] = ordered
            dlg.destroy()

        canvas.bind("<Button-1>", _on_click)
        canvas.bind("<Configure>", lambda _event: _redraw())

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Repair Selected Wells", width=22, command=_confirm).pack(side=tk.RIGHT)

        _refresh_summary()
        _redraw()
        dlg.wait_window()
        return result["selection"]

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
        """Keep the Arm Trigger button visually clear across acquisition states."""
        if not hasattr(self, "arm_btn"):
            return

        if not self.spectrometer or not self.spectrometer.is_connected:
            self._set_arm_button_visual("disabled")
            return

        if not self.spectrometer.capabilities.has_external_trigger:
            self._set_arm_button_visual("disabled")
            return

        if self.worker and self.worker.state == "ARMED":
            self._set_arm_button_visual("armed")
            return

        if self.worker and self.worker.state in ("LIVE", "TEST"):
            self._set_arm_button_visual("disabled")
            return

        self._set_arm_button_visual("ready")

    def _set_arm_button_visual(self, visual_state):
        """Show whether the trigger is ready, armed, or unavailable."""
        if not hasattr(self, "arm_btn"):
            return

        if visual_state == "armed":
            self.arm_btn.config(
                text="Armed | Waiting",
                style="ArmArmed.TButton",
                state="normal",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Armed and waiting for trigger")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg="#DDF3E5", fg="#143223")
        elif visual_state == "ready":
            self.arm_btn.config(
                text="Arm Trigger",
                style="ArmReady.TButton",
                state="normal",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Arm trigger before shooting")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg="#F6D9D8", fg="#7E1F2A")
        else:
            self.arm_btn.config(
                text="Arm Trigger",
                style="LeftAligned.TButton",
                state="disabled",
            )
            if hasattr(self, "arm_status_var"):
                self.arm_status_var.set("Trigger unavailable")
            if hasattr(self, "arm_status_chip"):
                self.arm_status_chip.config(bg="#E4E8ED", fg="#4D5A67")

    def _set_worker_state(self, state_text):
        """Update the worker state text and color-code armed vs not armed."""
        self.worker_state_var.set(state_text)
        if hasattr(self, "worker_state_label"):
            is_armed = "ARMED" in str(state_text).upper()
            self.worker_state_label.config(fg="#1A7F37" if is_armed else "#B42318")

    def _schedule_spectrum_draw(self):
        """Coalesce rapid spectrum updates into a capped redraw cadence."""
        if self._pending_spectrum is None or self._pending_draw_after_id is not None:
            return

        elapsed_ms = (time.perf_counter() - self._last_plot_draw_at) * 1000.0
        delay_ms = 0 if elapsed_ms >= self.live_redraw_interval_ms else int(self.live_redraw_interval_ms - elapsed_ms)
        self._pending_draw_after_id = self.root.after(delay_ms, self._flush_pending_spectrum)

    def _flush_pending_spectrum(self):
        """Draw the latest queued live spectrum."""
        self._pending_draw_after_id = None
        if self._pending_spectrum is None:
            return

        wavelengths, intensities = self._pending_spectrum
        self._pending_spectrum = None

        from acquisition_graph import update_spectrum_fast

        update_spectrum_fast(self.ax, self.canvas, self.live_line, wavelengths, intensities)
        self._last_plot_draw_at = time.perf_counter()

    def _cancel_pending_spectrum_draw(self, *, drop_pending: bool = False):
        """Cancel any scheduled coalesced redraw callback."""
        if self._pending_draw_after_id is not None:
            try:
                self.root.after_cancel(self._pending_draw_after_id)
            except tk.TclError:
                pass
            self._pending_draw_after_id = None
        if drop_pending:
            self._pending_spectrum = None

    def _cancel_canvas_idle_draw(self):
        """Cancel any Matplotlib Tk idle-draw callback before tearing down the UI."""
        if not hasattr(self, "canvas"):
            return
        idle_draw_id = getattr(self.canvas, "_idle_draw_id", None)
        if not idle_draw_id:
            return
        try:
            self.canvas._tkcanvas.after_cancel(idle_draw_id)
        except (AttributeError, tk.TclError):
            pass
        self.canvas._idle_draw_id = None

    def _queue_poll_delay_ms(self, processed_messages: int):
        """Poll faster while acquisition is active and slower when idle."""
        if self.worker is None:
            return self.idle_queue_poll_ms
        if processed_messages > 0 or self._pending_spectrum is not None:
            return self.active_queue_poll_ms
        if self.worker.state in {"LIVE", "ARMED", "TEST"}:
            return self.active_queue_poll_ms
        return self.idle_queue_poll_ms

    # ═══════════════════════════════════════════════════════════════════
    #  Message Queue Polling (thread-safe GUI updates)
    # ═══════════════════════════════════════════════════════════════════

    def _is_acquisition_busy(self):
        return bool(self.worker and self.worker.state != "IDLE")

    def _plate_payload_finished(self, payload):
        return bool(payload and (payload.get("complete") or payload.get("closed_early")))

    def _plate_requires_reconfigure(self):
        return bool(self.plate_mode_var.get() and self._plate_payload_finished(self.plate_progress))

    def _current_plate_runtime_settings(self):
        """Snapshot the acquisition settings that should be written into plate metadata."""
        averages = 1
        if hasattr(self, "averages_var"):
            try:
                averages = max(1, int(self.averages_var.get()))
            except (TypeError, ValueError):
                averages = 1

        return {
            "sample_name": self.sample_name_var.get().strip() or "Sample",
            "integration_time_ms": self.integration_var.get().strip() if hasattr(self, "integration_var") else "",
            "averages": averages,
            "correct_dark_counts": bool(self.correct_dark_var.get()) if hasattr(self, "correct_dark_var") else False,
            "correct_nonlinearity": bool(self.correct_nl_var.get()) if hasattr(self, "correct_nl_var") else False,
        }

    def _apply_plate_config(self, config):
        if not isinstance(config, PlateAutosaveConfig):
            config = PlateAutosaveConfig.from_mapping(config)

        self.plate_autosave_config = config
        self.plate_mode_var.set(True)
        self.configure_plate_btn.config(state="normal")
        self.auto_save_var.set(True)
        self.on_auto_save_toggle()
        self._pending_plate_run_state = None

        state = PlateRunState(config)
        payload = state.progress_payload()
        self._start_plate_history_card(payload)
        self._update_plate_progress(payload)
        if self.worker:
            self.worker.set_plate_autosave_config(config)

    def _apply_resumed_plate_state(self, state):
        if not isinstance(state, PlateRunState):
            raise TypeError("Expected a PlateRunState to resume plate autosave.")

        self.plate_autosave_config = state.config
        self._pending_plate_run_state = state
        self.plate_mode_var.set(True)
        self.configure_plate_btn.config(state="normal")
        self.auto_save_var.set(True)
        self.on_auto_save_toggle()

        payload = state.progress_payload()
        self.plate_history = [dict(payload)]
        self.current_plate_index = 0
        self._update_plate_progress(payload)

        if self.worker:
            self.worker.resume_plate_autosave(state)

        if payload["current_well"] is None:
            self.status_message_var.set(f"Loaded {state.config.plate_name}, which is already complete.")
        else:
            self.status_message_var.set(
                f"Resumed {state.config.plate_name}. Next position: {payload['current_well']}."
            )

    def _discover_resumable_plate_runs(self):
        return discover_resumable_plate_runs(self.save_dir_var.get())

    def _choose_resumable_plate(self, candidates):
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        result = {"candidate": None}
        dlg = tk.Toplevel(self.root)
        dlg.title("Resume Plate")
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.transient(self.root)

        dlg.update_idletasks()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        dw, dh = 760, 360
        dlg.geometry(f"{dw}x{dh}+{px + max((pw - dw) // 2, 0)}+{py + max((ph - dh) // 2, 0)}")

        outer = ttk.Frame(dlg, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="Select a plate folder to resume",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        columns = ("plate", "progress", "source")
        tree = ttk.Treeview(outer, columns=columns, show="headings", height=10)
        tree.heading("plate", text="Plate")
        tree.heading("progress", text="Progress")
        tree.heading("source", text="Source")
        tree.column("plate", width=220, anchor="w")
        tree.column("progress", width=360, anchor="w")
        tree.column("source", width=120, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        for index, candidate in enumerate(candidates):
            payload = candidate["payload"]
            next_well = payload["current_well"] or "complete"
            progress = (
                f"{payload['saved_shots']}/{payload['total_shots']} shots, "
                f"{payload['complete_wells']}/{payload['total_wells']} wells, next {next_well}"
            )
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(candidate["plate_name"], progress, candidate["source_label"]),
            )

        tree.selection_set("0")
        tree.focus("0")

        def _confirm(*_):
            selection = tree.selection()
            if not selection:
                return
            result["candidate"] = candidates[int(selection[0])]
            dlg.destroy()

        button_bar = ttk.Frame(outer)
        button_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text="Resume Selected", width=18, command=_confirm).pack(side=tk.RIGHT)

        tree.bind("<Double-1>", _confirm)
        dlg.wait_window()
        return result["candidate"]

    def _prompt_resume_plate_selection(self):
        candidates = self._discover_resumable_plate_runs()
        if not candidates:
            messagebox.showinfo(
                "No Resumable Plates",
                "No incomplete high-throughput plate folders were found in the current save directory.",
                parent=self.root,
            )
            return None

        candidate = self._choose_resumable_plate(candidates)
        if candidate is None:
            return None

        if not candidate.get("needs_confirmation"):
            return candidate["state"]

        suggested_config = candidate["state"].config
        confirmed = self._ask_plate_settings(
            initial_config=suggested_config,
            dialog_title="Resume Plate Settings",
            action_text="Resume Plate",
            allow_resume=False,
            helper_text=(
                "The files were scanned from disk. Review the inferred settings before resuming."
            ),
        )
        if confirmed is None:
            return None

        try:
            return PlateRunState.from_records(confirmed, candidate["records"])
        except ValueError as exc:
            messagebox.showerror("Resume Failed", str(exc), parent=self.root)
            return None

    def _ask_plate_settings(
        self,
        initial_config=None,
        dialog_title="High-Throughput Plate Settings",
        action_text="Use Plate Settings",
        allow_resume=True,
        helper_text="Files are saved into a subfolder named after the plate.",
    ):
        """Show the modal high-throughput plate settings dialog."""
        current = initial_config or self.plate_autosave_config or PlateAutosaveConfig()
        result = {"selection": None}

        dlg = tk.Toplevel(self.root)
        dlg.title(dialog_title)
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.transient(self.root)

        dlg.update_idletasks()
        screen_w = dlg.winfo_screenwidth()
        screen_h = dlg.winfo_screenheight()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        desired_w, desired_h = 760, 640
        min_w, min_h = 700, 580
        dw = max(min_w, min(desired_w, screen_w - 80))
        dh = max(min_h, min(desired_h, screen_h - 120))
        dx = px + max((pw - dw) // 2, 0)
        dy = py + max((ph - dh) // 2, 0)
        dx = max(20, min(dx, screen_w - dw - 20))
        dy = max(20, min(dy, screen_h - dh - 40))
        dlg.minsize(min_w, min_h)
        dlg.geometry(f"{dw}x{dh}+{dx}+{dy}")

        main = ttk.Frame(dlg, padding=14)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        ttk.Label(
            main,
            text=dialog_title,
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        form = ttk.LabelFrame(main, text="Run Settings", padding=10)
        form.grid(row=1, column=0, sticky="nsw", padx=(0, 12))

        plate_type_var = tk.StringVar(value=str(current.plate_type))
        plate_name_var = tk.StringVar(value=current.plate_name)
        shots_var = tk.StringVar(value=str(current.shots_per_well))
        order_var = tk.StringVar(value=current.order_mode)
        laser_wavelength_var = tk.StringVar(
            value="" if current.laser_wavelength_nm is None else str(current.laser_wavelength_nm)
        )
        laser_energy_default = current.laser_energy
        if not laser_energy_default and current.laser_energy_mj is not None:
            laser_energy_default = str(current.laser_energy_mj)
        laser_energy_var = tk.StringVar(value=laser_energy_default)
        laser_hz_var = tk.StringVar(value=current.laser_hz)
        delay_enabled_var = tk.BooleanVar(value=current.delay_enabled)
        delay_ms_var = tk.StringVar(value=current.delay_ms)

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
            text=helper_text,
            style="Status.TLabel",
            wraplength=210,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

        metadata_frame = ttk.LabelFrame(form, text="Laser Metadata", padding=8)
        metadata_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        metadata_frame.columnconfigure(1, weight=1)

        ttk.Label(metadata_frame, text="Laser wavelength (nm):", style="Status.TLabel").grid(
            row=0, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_wavelength_var, width=16).grid(
            row=0, column=1, sticky="ew", pady=3
        )

        ttk.Label(metadata_frame, text="Laser energy:", style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_energy_var, width=16).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        ttk.Label(metadata_frame, text="Laser frequency (Hz):", style="Status.TLabel").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Entry(metadata_frame, textvariable=laser_hz_var, width=16).grid(
            row=2, column=1, sticky="ew", pady=3
        )

        ttk.Checkbutton(
            metadata_frame,
            text="Delay enabled",
            variable=delay_enabled_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 3))

        ttk.Label(metadata_frame, text="Delay (ms):", style="Status.TLabel").grid(
            row=4, column=0, sticky="w", pady=3
        )
        delay_ms_entry = ttk.Entry(metadata_frame, textvariable=delay_ms_var, width=16)
        delay_ms_entry.grid(row=4, column=1, sticky="ew", pady=3)

        runtime_settings_var = tk.StringVar()
        runtime_frame = ttk.LabelFrame(form, text="Spectrometer Settings Saved Automatically", padding=8)
        runtime_frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Label(
            runtime_frame,
            textvariable=runtime_settings_var,
            style="Status.TLabel",
            wraplength=210,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="w")

        preview_frame = ttk.LabelFrame(main, text="Plate Preview", padding=10)
        preview_frame.grid(row=1, column=1, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview_canvas = tk.Canvas(
            preview_frame,
            width=420,
            height=320,
            bg="#F7F9FC",
            highlightthickness=1,
            highlightbackground="#C8D0DA",
        )
        preview_canvas.grid(row=0, column=0, sticky="nsew")

        button_bar = ttk.Frame(main)
        button_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        def _current_dialog_config():
            runtime = self._current_plate_runtime_settings()
            return PlateAutosaveConfig.from_mapping({
                "plate_type": plate_type_var.get(),
                "plate_name": plate_name_var.get(),
                "shots_per_well": shots_var.get(),
                "order_mode": order_var.get(),
                "laser_wavelength_nm": laser_wavelength_var.get(),
                "laser_energy_mj": laser_energy_var.get(),
                "laser_energy": laser_energy_var.get(),
                "laser_hz": laser_hz_var.get(),
                "delay_enabled": delay_enabled_var.get(),
                "delay_ms": delay_ms_var.get(),
                **runtime,
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

            runtime = self._current_plate_runtime_settings()
            runtime_settings_var.set(
                "\n".join([
                    f"Sample name: {runtime['sample_name']}",
                    f"Integration time: {runtime['integration_time_ms'] or 'Unknown'} ms",
                    f"Averages: {runtime['averages']}",
                    f"Dark correction: {'On' if runtime['correct_dark_counts'] else 'Off'}",
                    f"Nonlinearity correction: {'On' if runtime['correct_nonlinearity'] else 'Off'}",
                ])
            )

            delay_ms_entry.config(state="normal" if delay_enabled_var.get() else "disabled")

        def _apply():
            try:
                config = _current_dialog_config()
            except Exception:
                messagebox.showwarning("Invalid Plate Settings", "Please enter a valid shot count.", parent=dlg)
                return
            result["selection"] = config
            dlg.destroy()

        def _resume():
            selection = self._prompt_resume_plate_selection()
            if selection is None:
                return
            result["selection"] = selection
            dlg.destroy()

        for var in (
            plate_type_var,
            plate_name_var,
            shots_var,
            order_var,
            laser_wavelength_var,
            laser_energy_var,
            laser_hz_var,
            delay_ms_var,
        ):
            var.trace_add("write", _redraw_preview)
        delay_enabled_var.trace_add("write", _redraw_preview)

        ttk.Button(button_bar, text="Cancel", width=12, command=dlg.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_bar, text=action_text, width=18, command=_apply).pack(side=tk.RIGHT)
        if allow_resume:
            ttk.Button(button_bar, text="Resume Existing Plate...", width=22, command=_resume).pack(
                side=tk.LEFT
            )

        _redraw_preview()
        dlg.wait_window()
        return result["selection"]

    def _update_plate_progress(self, payload):
        self.plate_progress = payload
        if not payload:
            return

        self._update_plate_history_card(payload)
        if payload.get("repair_active"):
            queue_text = ", ".join(payload.get("repair_queue", [])[:3])
            if len(payload.get("repair_queue", [])) > 3:
                queue_text += f", +{len(payload['repair_queue']) - 3} more"
            resume_well = payload.get("repair_resume_well") or "plate completion"
            next_text = f"repairing {queue_text}, then resume {resume_well}"
        elif payload["current_well"] is None:
            next_text = "finished early" if payload.get("closed_early") else "complete"
        else:
            next_text = f"next {payload['current_well']}"

        self.plate_progress_var.set(
            f"{payload['plate_name']} ({payload['plate_type']}-well): "
            f"{payload['complete_wells']}/{payload['total_wells']} wells, {next_text}"
        )
        self._show_plate_overview()
        self._redraw_plate_history()
        finished = self._plate_payload_finished(payload)
        self.discard_plate_shot_btn.config(
            state="normal" if payload.get("can_discard") and not finished else "disabled"
        )
        self.repair_plate_wells_btn.config(
            state="normal"
            if payload.get("saved_shots", 0) and not finished and not payload.get("repair_active")
            else "disabled"
        )
        self.finish_plate_btn.config(
            state="normal"
            if payload.get("saved_shots", 0) and not finished and not payload.get("repair_active")
            else "disabled"
        )

    def _start_plate_history_card(self, payload):
        if self.current_plate_index is None:
            self.plate_history = [dict(payload)]
            self.current_plate_index = 0
            return

        current = self.plate_history[self.current_plate_index]
        if self._plate_payload_finished(current):
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

    def _prompt_for_next_plate(self):
        """Offer to configure the next plate after the current one completes."""
        if not self.plate_autosave_config:
            return
        if self.worker:
            self.worker.disable_plate_autosave()

        if not messagebox.askyesno(
            "Plate Complete",
            "The current plate is complete.\n\nConfigure the next plate now?",
            parent=self.root,
        ):
            self.status_message_var.set("Plate complete. Click Configure Plate to start the next plate.")
            return

        selection = self._ask_plate_settings()
        if selection is None:
            self.status_message_var.set("Plate complete. Click Configure Plate to start the next plate.")
            return

        if isinstance(selection, PlateRunState):
            self._apply_resumed_plate_state(selection)
        else:
            self._apply_plate_config(selection)

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
            if index == self.current_plate_index and not self._plate_payload_finished(payload):
                title += " (current)"
            elif payload.get("closed_early"):
                title += " (finished early)"
            elif payload.get("complete"):
                title += " (done)"
            elif self.current_plate_index is not None and index > self.current_plate_index:
                title += " (next)"

            card = ttk.LabelFrame(self.plate_history_inner, text=title, padding=(6, 4))
            card.grid(row=0, column=index, sticky="n", padx=(0, 10), pady=2)
            plate_canvas = tk.Canvas(
                card,
                width=520,
                height=360,
                bg="#F7F9FC",
                highlightthickness=1,
                highlightbackground="#C8D0DA",
            )
            plate_canvas.pack(fill=tk.BOTH, expand=True)
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

    def _plate_layout_metrics(self, payload, width, height, preview=False):
        rows = payload["rows"]
        columns = payload["columns"]
        left_bound = 62 if preview else 58
        top_bound = 58 if preview else 56
        right_bound = width - 20
        bottom_bound = height - 20
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
        return {
            "rows": rows,
            "columns": columns,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "cell_w": cell_w,
            "cell_h": cell_h,
            "min_cell": min(cell_w, cell_h),
        }

    def _well_from_canvas_point(self, payload, x, y, preview=False, canvas_width=None, canvas_height=None):
        width = max(1, int(canvas_width or 0))
        height = max(1, int(canvas_height or 0))
        if width <= 1 or height <= 1:
            width = 700 if not preview else 420
            height = 430 if not preview else 320
        metrics = self._plate_layout_metrics(payload, width, height, preview=preview)
        if not (metrics["left"] <= x <= metrics["right"] and metrics["top"] <= y <= metrics["bottom"]):
            return None
        column = int((x - metrics["left"]) / metrics["cell_w"]) + 1
        row = int((y - metrics["top"]) / metrics["cell_h"])
        if row < 0 or row >= metrics["rows"] or column < 1 or column > metrics["columns"]:
            return None
        return f"{chr(65 + row)}{column}"

    def _draw_plate_payload(self, canvas, payload, preview=False, selected_wells=None, disabled_wells=None):
        canvas.delete("all")

        width = max(canvas.winfo_width(), int(canvas.cget("width") or 1))
        height = max(canvas.winfo_height(), int(canvas.cget("height") or 1))
        scale = 3
        rows = payload["rows"]
        columns = payload["columns"]
        shots_per_well = payload["shots_per_well"]
        shots_by_well = payload["shots_by_well"]
        current_well = payload["current_well"]
        selected_wells = set(selected_wells or [])
        disabled_wells = set(disabled_wells or [])
        repair_queue = list(payload.get("repair_queue", []))
        repaired_wells = set(payload.get("repaired_wells", []))

        image = Image.new("RGB", (width * scale, height * scale), "#F7F9FC")
        draw = ImageDraw.Draw(image)

        def px(value):
            return int(round(value * scale))

        def xy(x, y):
            return (px(x), px(y))

        def font(size, bold=False):
            font_name = "segoeuib.ttf" if bold else "segoeui.ttf"
            try:
                return ImageFont.truetype(font_name, max(8, px(size)))
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
        draw.text(xy(10, 12), title, anchor="lt", fill="#1E2B36", font=font(11, bold=True))

        metrics = self._plate_layout_metrics(payload, width, height, preview=preview)
        left = metrics["left"]
        top = metrics["top"]
        right = metrics["right"]
        bottom = metrics["bottom"]
        cell_w = metrics["cell_w"]
        cell_h = metrics["cell_h"]
        min_cell = metrics["min_cell"]
        radius = max(2, min_cell * 0.36)
        count_font_size = 11 if min_cell >= 18 else 9
        show_counts = min_cell >= 10
        show_edge_labels = min_cell >= 8
        edge_font = font(12 if min_cell >= 20 else 10, bold=True)
        count_font = font(count_font_size, bold=True)

        if payload["order_mode"] == ORDER_ROW:
            draw_arrow_line((left + cell_w * 0.2, top - 30), (right - cell_w * 0.2, top - 30), "#1D6FB8")
        else:
            draw_arrow_line((left - 32, top + cell_h * 0.2), (left - 32, bottom - cell_h * 0.2), "#1D6FB8")

        if show_edge_labels:
            for column in range(1, columns + 1):
                cx = left + (column - 0.5) * cell_w
                draw.text(xy(cx, top - 17), str(column), anchor="mm", fill="#1E3140", font=edge_font)
            for row in range(rows):
                cy = top + (row + 0.5) * cell_h
                draw.text(xy(left - 17, cy), chr(65 + row), anchor="mm", fill="#1E3140", font=edge_font)

        for row in range(rows):
            for column in range(1, columns + 1):
                well = f"{chr(65 + row)}{column}"
                count = shots_by_well.get(well, 0)
                cx = left + (column - 0.5) * cell_w
                cy = top + (row + 0.5) * cell_h
                fill = "#DDE5EE"
                outline = "#9BA8B4"
                width_px = 1
                text_fill = "#14212B"
                if count >= shots_per_well:
                    fill = "#55B97D"
                    outline = "#2F7E50"
                elif count > 0:
                    fill = "#F3C760"
                    outline = "#A87A16"
                if well in repair_queue:
                    fill = "#F3C760"
                    outline = "#8C6714"
                    width_px = 2
                if well in repaired_wells and count >= shots_per_well:
                    fill = "#55B97D"
                    outline = "#1F5E3C"
                    width_px = 2
                if well in selected_wells:
                    fill = "#DDEBFA"
                    outline = "#1F5D96"
                    width_px = 3
                if well in disabled_wells:
                    fill = "#EEF2F5"
                    outline = "#C4CDD6"
                    text_fill = "#8A99A8"
                if well == current_well and well not in repair_queue:
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
                    draw.text(xy(cx, cy), str(count), anchor="mm", fill=text_fill, font=count_font)

        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        photo = ImageTk.PhotoImage(image.resize((width, height), resample))
        canvas.create_image(0, 0, image=photo, anchor=tk.NW)
        canvas._plate_image = photo

    def _poll_queue(self, reschedule: bool = True):
        """Check the worker's message queue and process all pending messages."""
        if self.worker is None:
            return

        from acquisition_worker import AcquisitionMessage
        from acquisition_graph import highlight_captured_spectrum
        processed_messages = 0

        try:
            while True:
                msg_type, data = self.worker.message_queue.get_nowait()
                processed_messages += 1

                if msg_type == AcquisitionMessage.SPECTRUM:
                    wavelengths, intensities = data
                    self.current_wavelengths = wavelengths
                    self.current_intensities = intensities
                    self._pending_spectrum = (wavelengths, intensities)
                    self._schedule_spectrum_draw()
                    # Enable save/send buttons now that we have data
                    self.save_spectrum_btn.config(state="normal")
                    self.send_to_analysis_btn.config(state="normal")

                elif msg_type == AcquisitionMessage.STATUS:
                    self.status_message_var.set(str(data))

                elif msg_type == AcquisitionMessage.ERROR:
                    self.status_message_var.set(f"Error: {data}")
                    logger.error(data)

                elif msg_type == AcquisitionMessage.ARMED:
                    self._set_arm_button_visual("armed")
                    self._set_worker_state("State: ARMED")

                elif msg_type == AcquisitionMessage.CAPTURED:
                    self._cancel_pending_spectrum_draw(drop_pending=True)
                    shot_idx = data["shot_index"]
                    self.current_wavelengths = data["wavelengths"]
                    self.current_intensities = data["intensities"]
                    self.shot_count_var.set(f"Shots: {shot_idx}")
                    # Visual feedback
                    self._cancel_highlight_timer()
                    highlight_captured_spectrum(
                        self.ax, self.canvas, self.live_line, data["wavelengths"],
                        data["intensities"], shot_idx
                    )
                    self._last_plot_draw_at = time.perf_counter()
                    # Remove highlight after 2 seconds
                    self._highlight_after_id = self.root.after(2000, self._remove_highlight)

                    # The worker automatically re-arms after each capture.
                    # While it stays in ARMED state, keep Stop enabled.
                    if self.worker and self.worker.state == "ARMED":
                        self._set_arm_button_visual("armed")
                        self._set_worker_state(f"State: ARMED (shot {shot_idx})")
                    else:
                        # Worker returned to idle (error or single-shot test)
                        self.live_btn.config(state="normal")
                        self._update_arm_btn_state()
                        self.test_trigger_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                        self._set_worker_state("State: IDLE")

                elif msg_type == AcquisitionMessage.IDLE:
                    self._remove_highlight()
                    # Worker returned to idle — restore button state
                    self.live_btn.config(state="normal")
                    self._update_arm_btn_state()
                    self.test_trigger_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self._set_worker_state("State: IDLE")
                    if self._plate_completion_prompt_pending:
                        self._plate_completion_prompt_pending = False
                        self.root.after(0, self._prompt_for_next_plate)

                elif msg_type == AcquisitionMessage.SAVE_COMPLETE:
                    self.status_message_var.set(f"Saved: {os.path.basename(data)}")

                elif msg_type == AcquisitionMessage.TIMING:
                    sample = dict(data)
                    sample["gui_received_at"] = time.perf_counter()
                    if "worker_enqueued_at" in sample:
                        latency_s = sample["gui_received_at"] - sample["worker_enqueued_at"]
                        sample["gui_queue_latency"] = latency_s
                        sample["gui_queue_latency_ms"] = latency_s * 1000.0
                    self.latest_timing_sample = sample
                    self.timing_samples.append(sample)

                elif msg_type == AcquisitionMessage.PLATE_PROGRESS:
                    self._update_plate_progress(data)

                elif msg_type == AcquisitionMessage.PLATE_DISCARDED:
                    self._update_plate_progress(data)
                    discarded = data.get("discarded")
                    if discarded:
                        self.status_message_var.set(f"Discarded: {os.path.basename(discarded)}")

                elif msg_type == AcquisitionMessage.PLATE_REPAIR_STARTED:
                    self._update_plate_progress(data)
                    queued = data.get("repair_queue", [])
                    if queued:
                        self.status_message_var.set(f"Repairing wells: {self._format_well_list(queued)}")

                elif msg_type == AcquisitionMessage.PLATE_REPAIR_COMPLETE:
                    self._update_plate_progress(data)
                    next_well = data.get("current_well") or "plate completion"
                    self.status_message_var.set(f"Repair pass complete. Resuming {next_well}.")

                elif msg_type == AcquisitionMessage.PLATE_COMPLETE:
                    self._update_plate_progress(data)
                    self.status_message_var.set("Plate complete.")
                    self._plate_completion_prompt_pending = True

                elif msg_type == AcquisitionMessage.STOPPED:
                    self._remove_highlight()
                    self._set_arm_button_visual("disabled")
                    self._set_worker_state("State: STOPPED")

        except queue.Empty:
            pass

        # Schedule next poll with a faster cadence while acquisition is active.
        if reschedule and self.worker:
            self._queue_poll_after_id = self.root.after(
                self._queue_poll_delay_ms(processed_messages),
                self._poll_queue,
            )

    def _cancel_queue_poll(self):
        """Cancel any pending queue polling callback."""
        if self._queue_poll_after_id is None:
            self._cancel_pending_spectrum_draw(drop_pending=True)
            return
        try:
            self.root.after_cancel(self._queue_poll_after_id)
        except tk.TclError:
            pass
        self._queue_poll_after_id = None
        self._cancel_pending_spectrum_draw(drop_pending=True)

    def _cancel_highlight_timer(self):
        """Cancel any pending highlight clear callback."""
        if self._highlight_after_id is not None:
            try:
                self.root.after_cancel(self._highlight_after_id)
            except tk.TclError:
                pass
            self._highlight_after_id = None

    def _remove_highlight(self):
        """Restore the live line after a capture highlight."""
        self._cancel_highlight_timer()
        if self._highlight_line:
            from acquisition_graph import clear_highlight
            clear_highlight(self.ax, self.canvas, self._highlight_line)

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
        self._cancel_queue_poll()
        self._cancel_canvas_idle_draw()
        self._remove_highlight()
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            pass

        # Remove all acquisition widgets so the root can be reused
        for widget in self.root.winfo_children():
            widget.destroy()

        self.root.quit()

    def _cleanup_and_close(self):
        """Stop the worker, disconnect the spectrometer, and exit.
        Used when the user closes the window without handoff."""
        self._cancel_queue_poll()
        self._cancel_canvas_idle_draw()
        self._remove_highlight()
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None

        if self.spectrometer:
            self.spectrometer.disconnect()
            self.spectrometer = None

        try:
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            pass

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
