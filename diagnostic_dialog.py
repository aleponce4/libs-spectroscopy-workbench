# diagnostic_dialog.py - Connection diagnostics & multi-device picker dialog.
#
# Shows a detailed breakdown of what was found on the USB bus, which drivers
# are loaded, which devices seabreeze / VISA can actually talk to, and lets
# the user pick a specific device or fall back to simulation.

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import logging

logger = logging.getLogger(__name__)


class DiagnosticDialog:
    """
    Modal dialog that runs spectrometer diagnostics and presents results
    alongside a device picker.

    Usage::

        dlg = DiagnosticDialog(parent_root)
        result = dlg.result   # see below

    ``result`` is one of:
        - ``None``                     – user cancelled
        - ``{"action": "simulate", "brand": "ocean_optics" | "thorlabs"}``
        - ``{"action": "connect",  "brand": ..., "device_index": int}``
        - ``{"action": "connect_resource", "brand": "thorlabs", "resource": str}``
    """

    def __init__(self, parent, auto_reason: str | None = None):
        """
        Args:
            parent:      Tk root or Toplevel to be modal over.
            auto_reason: If set, shown at the top as the reason diagnostics ran
                         automatically (e.g. "No spectrometer found").
        """
        self.parent = parent
        self.result = None

        self._ocean_report: dict | None = None
        self._thorlabs_report: dict | None = None
        self._scan_done = False

        # ── Create window ──────────────────────────────────────────────
        self.dlg = tk.Toplevel(parent)
        self.dlg.title("Spectrometer Diagnostics")
        self.dlg.resizable(True, True)
        self.dlg.grab_set()
        self.dlg.transient(parent)
        self.dlg.minsize(640, 420)
        self.dlg.geometry("720x560")
        self._center_on_parent()

        # ── Top frame: reason banner ───────────────────────────────────
        if auto_reason:
            banner = ttk.Frame(self.dlg, padding=8)
            banner.pack(fill=tk.X)
            ttk.Label(
                banner,
                text=f"⚠  {auto_reason}",
                font=("Segoe UI", 10, "bold"),
                foreground="#B85C00",
                wraplength=680,
            ).pack(anchor="w")
            ttk.Separator(self.dlg, orient="horizontal").pack(fill=tk.X)

        # ── Notebook (tabs) ────────────────────────────────────────────
        self._notebook = ttk.Notebook(self.dlg)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))

        # Tab 1: Ocean Optics
        self._ocean_tab = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(self._ocean_tab, text="Ocean Optics")

        # Tab 2: Thorlabs
        self._thorlabs_tab = ttk.Frame(self._notebook, padding=8)
        self._notebook.add(self._thorlabs_tab, text="Thorlabs CCS")

        # Placeholder "Scanning…" labels
        self._ocean_scanning_label = ttk.Label(
            self._ocean_tab, text="Scanning USB bus and drivers…",
            font=("Segoe UI", 10),
        )
        self._ocean_scanning_label.pack(pady=30)

        self._thorlabs_scanning_label = ttk.Label(
            self._thorlabs_tab, text="Scanning USB bus and drivers…",
            font=("Segoe UI", 10),
        )
        self._thorlabs_scanning_label.pack(pady=30)

        # ── Bottom button bar ──────────────────────────────────────────
        btn_bar = ttk.Frame(self.dlg, padding=8)
        btn_bar.pack(fill=tk.X)

        self._sim_btn = ttk.Button(
            btn_bar, text="Simulation Mode", width=18,
            command=self._on_simulate, state="disabled",
        )
        self._sim_btn.pack(side=tk.LEFT, padx=5)

        self._copy_btn = ttk.Button(
            btn_bar, text="Copy Log", width=12,
            command=self._on_copy_log, state="disabled",
        )
        self._copy_btn.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_bar, text="Cancel", width=10, command=self._on_cancel,
        ).pack(side=tk.RIGHT, padx=5)

        # Placeholder for the "Connect" button — created once scan finishes
        self._connect_btn = None

        # ── Kick off background scan ───────────────────────────────────
        self._scan_thread = threading.Thread(
            target=self._run_diagnostics, daemon=True
        )
        self._scan_thread.start()
        self._poll_scan()

        # ── Wait ───────────────────────────────────────────────────────
        self.dlg.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.dlg.wait_window()

    # ════════════════════════════════════════════════════════════════════
    #  Background scan
    # ════════════════════════════════════════════════════════════════════

    def _run_diagnostics(self):
        """Runs in a background thread — imports can be slow."""
        from spectrometer import SpectrometerModule, ThorlabsCCSModule

        try:
            self._ocean_report = SpectrometerModule.diagnose()
        except Exception as e:
            self._ocean_report = {
                "backend": "SpectrometerModule",
                "notes": [f"Diagnostic scan crashed: {e}"],
                "seabreeze_devices": [],
                "usb_devices": [],
                "driver_warnings": [],
                "per_device_errors": {},
            }

        try:
            self._thorlabs_report = ThorlabsCCSModule.diagnose()
        except Exception as e:
            self._thorlabs_report = {
                "backend": "ThorlabsCCSModule",
                "notes": [f"Diagnostic scan crashed: {e}"],
                "usb_devices": [],
                "visa_resources": [],
            }

        self._scan_done = True

    def _poll_scan(self):
        """Poll every 200 ms until the background scan finishes."""
        if self._scan_done:
            self._populate_results()
        else:
            self.dlg.after(200, self._poll_scan)

    # ════════════════════════════════════════════════════════════════════
    #  Populate results into the two tabs
    # ════════════════════════════════════════════════════════════════════

    def _populate_results(self):
        self._ocean_scanning_label.destroy()
        self._thorlabs_scanning_label.destroy()

        self._populate_ocean_tab()
        self._populate_thorlabs_tab()

        self._sim_btn.config(state="normal")
        self._copy_btn.config(state="normal")

    # ── Ocean Optics tab ───────────────────────────────────────────────

    def _populate_ocean_tab(self):
        tab = self._ocean_tab
        rpt = self._ocean_report or {}

        row = 0

        # Backend info
        backend = rpt.get("seabreeze_backend")
        installed = rpt.get("seabreeze_installed", False)
        if not installed:
            ttk.Label(tab, text="✗  python-seabreeze is NOT installed",
                      foreground="red", font=("Segoe UI", 10, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            row += 1
        else:
            backend_text = backend or "none loaded"
            fail_reason = rpt.get("seabreeze_backend_fail_reason") or ""
            lbl_color = "#006600" if backend else "red"
            ttk.Label(tab, text=f"Seabreeze backend:  {backend_text}",
                      foreground=lbl_color, font=("Segoe UI", 10, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            row += 1
            if fail_reason:
                ttk.Label(tab, text=f"   ↳ {fail_reason}",
                          foreground="#886600", wraplength=650,
                          font=("Segoe UI", 9)
                          ).grid(row=row, column=0, columnspan=3, sticky="w")
                row += 1

        row += 1  # spacer

        # USB bus devices
        usb_devs = rpt.get("usb_devices", [])
        ttk.Label(tab, text=f"USB bus  (VID 0x2457 — Ocean Optics):  "
                            f"{len(usb_devs)} device(s)",
                  font=("Segoe UI", 10, "bold")
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
        row += 1

        if usb_devs:
            for ud in usb_devs:
                status_icon = "✓" if ud["status"] == "OK" else "✗"
                ttk.Label(tab, text=f"  {status_icon}  PID 0x{ud['pid']}  —  "
                                    f"{ud['description']}",
                          font=("Segoe UI", 9)
                          ).grid(row=row, column=0, columnspan=3, sticky="w")
                row += 1
                ttk.Label(tab, text=f"       Driver: {ud['driver']}  |  "
                                    f"Status: {ud['status']}",
                          font=("Segoe UI", 9), foreground="gray"
                          ).grid(row=row, column=0, columnspan=3, sticky="w")
                row += 1
        else:
            ttk.Label(tab, text="  (none detected on USB bus)",
                      foreground="gray", font=("Segoe UI", 9)
                      ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        # Driver warnings
        for dw in rpt.get("driver_warnings", []):
            ttk.Label(tab, text=f"  ⚠  {dw['device']}:  {dw['advice']}",
                      foreground="#CC6600", wraplength=650,
                      font=("Segoe UI", 9)
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            row += 1

        row += 1  # spacer

        # Seabreeze devices (connectable)
        sb_devs = rpt.get("seabreeze_devices", [])
        ttk.Label(tab, text=f"Seabreeze device list:  {len(sb_devs)} device(s)",
                  font=("Segoe UI", 10, "bold")
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
        row += 1

        per_errors = rpt.get("per_device_errors", {})

        self._ocean_device_vars: list[tuple[int, str, str]] = []  # (index, model, serial)

        if sb_devs:
            self._ocean_selected = tk.IntVar(value=0)
            for dev in sb_devs:
                idx = dev["index"]
                model = dev["model"]
                serial = dev["serial"]
                err = per_errors.get(serial)
                self._ocean_device_vars.append((idx, model, serial))

                if err:
                    label_text = f"{model}  (S/N: {serial})  —  ✗ {err}"
                    fg = "red"
                else:
                    label_text = f"{model}  (S/N: {serial})  —  ✓ connectable"
                    fg = "#006600"

                rb = ttk.Radiobutton(
                    tab, text=label_text, value=idx,
                    variable=self._ocean_selected,
                )
                rb.grid(row=row, column=0, columnspan=3, sticky="w", padx=10)
                row += 1

            # Connect button
            connect_btn = ttk.Button(
                tab, text="Connect Selected", width=18,
                command=lambda: self._on_connect_ocean(),
            )
            connect_btn.grid(row=row, column=0, padx=10, pady=(8, 4), sticky="w")
            row += 1
        else:
            ttk.Label(tab, text="  (no devices found by seabreeze)",
                      foreground="gray", font=("Segoe UI", 9)
                      ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        # Notes
        notes = rpt.get("notes", [])
        if notes:
            row += 1
            ttk.Separator(tab, orient="horizontal").grid(
                row=row, column=0, columnspan=3, sticky="ew", pady=6
            )
            row += 1
            for note in notes:
                ttk.Label(tab, text=f"ℹ  {note}", wraplength=650,
                          foreground="#555555", font=("Segoe UI", 9)
                          ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
                row += 1

    # ── Thorlabs tab ───────────────────────────────────────────────────

    def _populate_thorlabs_tab(self):
        tab = self._thorlabs_tab
        rpt = self._thorlabs_report or {}

        row = 0

        # DLL status
        dll_found = rpt.get("dll_found", False)
        dll_path = rpt.get("dll_path")
        if dll_found:
            ttk.Label(tab, text=f"✓  TLCCS_64.dll found:  {dll_path}",
                      foreground="#006600", font=("Segoe UI", 10, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        else:
            ttk.Label(tab, text="✗  TLCCS_64.dll NOT found",
                      foreground="red", font=("Segoe UI", 10, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        # VISA status
        visa = rpt.get("visa_installed", False)
        if visa:
            ttk.Label(tab, text="✓  NI-VISA runtime found",
                      foreground="#006600", font=("Segoe UI", 10)
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        else:
            ttk.Label(tab, text="✗  NI-VISA runtime NOT found",
                      foreground="red", font=("Segoe UI", 10)
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        row += 1  # spacer

        # USB bus devices
        usb_devs = rpt.get("usb_devices", [])
        ttk.Label(tab, text=f"USB bus  (VID 0x1313 — Thorlabs):  "
                            f"{len(usb_devs)} device(s)",
                  font=("Segoe UI", 10, "bold")
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
        row += 1

        if usb_devs:
            for ud in usb_devs:
                status_icon = "✓" if ud["status"] == "OK" else "✗"
                ttk.Label(tab, text=f"  {status_icon}  PID 0x{ud['pid']}  —  "
                                    f"{ud['description']}",
                          font=("Segoe UI", 9)
                          ).grid(row=row, column=0, columnspan=3, sticky="w")
                row += 1
                ttk.Label(tab, text=f"       Driver: {ud['driver']}  |  "
                                    f"Status: {ud['status']}",
                          font=("Segoe UI", 9), foreground="gray"
                          ).grid(row=row, column=0, columnspan=3, sticky="w")
                row += 1
        else:
            ttk.Label(tab, text="  (none detected on USB bus)",
                      foreground="gray", font=("Segoe UI", 9)
                      ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        row += 1  # spacer

        # VISA resources (connectable)
        visa_res = rpt.get("visa_resources", [])
        ttk.Label(tab, text=f"VISA resources:  {len(visa_res)} device(s)",
                  font=("Segoe UI", 10, "bold")
                  ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
        row += 1

        self._thorlabs_resource_vars: list[tuple[str, str]] = []  # (model, resource)

        if visa_res:
            self._thorlabs_selected = tk.IntVar(value=0)
            for i, vr in enumerate(visa_res):
                model = vr["model"]
                resource = vr["resource"]
                self._thorlabs_resource_vars.append((model, resource))

                rb = ttk.Radiobutton(
                    tab,
                    text=f"{model}  —  {resource}",
                    value=i,
                    variable=self._thorlabs_selected,
                )
                rb.grid(row=row, column=0, columnspan=3, sticky="w", padx=10)
                row += 1

            connect_btn = ttk.Button(
                tab, text="Connect Selected", width=18,
                command=lambda: self._on_connect_thorlabs(),
            )
            connect_btn.grid(row=row, column=0, padx=10, pady=(8, 4), sticky="w")
            row += 1
        else:
            ttk.Label(tab, text="  (no VISA resources found)",
                      foreground="gray", font=("Segoe UI", 9)
                      ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        # Notes
        notes = rpt.get("notes", [])
        if notes:
            row += 1
            ttk.Separator(tab, orient="horizontal").grid(
                row=row, column=0, columnspan=3, sticky="ew", pady=6
            )
            row += 1
            for note in notes:
                ttk.Label(tab, text=f"ℹ  {note}", wraplength=650,
                          foreground="#555555", font=("Segoe UI", 9)
                          ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
                row += 1

    # ════════════════════════════════════════════════════════════════════
    #  Button handlers
    # ════════════════════════════════════════════════════════════════════

    def _on_connect_ocean(self):
        idx = self._ocean_selected.get()
        self.result = {
            "action": "connect",
            "brand": "ocean_optics",
            "device_index": idx,
        }
        self.dlg.destroy()

    def _on_connect_thorlabs(self):
        idx = self._thorlabs_selected.get()
        if idx < len(self._thorlabs_resource_vars):
            _, resource = self._thorlabs_resource_vars[idx]
            self.result = {
                "action": "connect_resource",
                "brand": "thorlabs",
                "resource": resource,
            }
        self.dlg.destroy()

    def _on_simulate(self):
        # Pick brand from whichever tab is active
        active_tab = self._notebook.index(self._notebook.select())
        brand = "ocean_optics" if active_tab == 0 else "thorlabs"
        self.result = {"action": "simulate", "brand": brand}
        self.dlg.destroy()

    def _on_cancel(self):
        self.result = None
        self.dlg.destroy()

    def _on_copy_log(self):
        """Copy the full diagnostic log to clipboard."""
        lines = ["=== Spectrometer Diagnostic Report ===\n"]

        for label, rpt in [("Ocean Optics", self._ocean_report),
                           ("Thorlabs CCS", self._thorlabs_report)]:
            lines.append(f"\n--- {label} ---")
            if rpt is None:
                lines.append("  (scan did not complete)")
                continue
            for key, val in rpt.items():
                if key == "notes":
                    for note in val:
                        lines.append(f"  NOTE: {note}")
                elif key == "driver_warnings":
                    for dw in val:
                        lines.append(f"  WARN: {dw.get('device')}: {dw.get('advice')}")
                elif key == "per_device_errors":
                    for serial, err in val.items():
                        lines.append(f"  ERR [{serial}]: {err}")
                elif isinstance(val, list):
                    lines.append(f"  {key}:")
                    for item in val:
                        lines.append(f"    {item}")
                else:
                    lines.append(f"  {key}: {val}")

        text = "\n".join(lines)
        self.dlg.clipboard_clear()
        self.dlg.clipboard_append(text)
        messagebox.showinfo("Copied", "Diagnostic log copied to clipboard.",
                            parent=self.dlg)

    # ════════════════════════════════════════════════════════════════════
    #  Helpers
    # ════════════════════════════════════════════════════════════════════

    def _center_on_parent(self):
        self.dlg.update_idletasks()
        pw = self.parent.winfo_width()
        ph = self.parent.winfo_height()
        px = self.parent.winfo_x()
        py = self.parent.winfo_y()
        dw = self.dlg.winfo_width()
        dh = self.dlg.winfo_height()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        self.dlg.geometry(f"+{x}+{y}")
