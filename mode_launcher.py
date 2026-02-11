# mode_launcher.py - Initial mode selection window shown before the main application loads.
# Allows the user to choose between Analysis Mode and Acquisition Mode.

import tkinter as tk
from tkinter import ttk
import platform
import sys
import os
from PIL import Image, ImageTk


class ModeLauncher:
    """A launcher window that lets the user choose between Analysis and Acquisition mode."""

    def __init__(self):
        # DPI awareness on Windows
        if platform.system() == 'Windows':
            from ctypes import windll  # type: ignore
            windll.shcore.SetProcessDpiAwareness(1)

        self.selected_mode = None

        # Create a plain root window (no sv_ttk — avoids poisoning
        # the Tcl interpreter cache for the next themed window)
        self.root = tk.Tk()
        self.root.title("LIBS Software - Select Mode")
        self.root.resizable(False, False)

        # Set the icon
        try:
            self.root.iconbitmap('Icons/main_icon.ico')
        except Exception:
            pass

        # Center the window
        window_width = 560
        window_height = 480
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()

    def _build_ui(self):
        """Build the launcher UI."""
        # Main container
        main_frame = ttk.Frame(self.root, padding=30)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 11))
        style.configure("ModeTitle.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("ModeDesc.TLabel", font=("Segoe UI", 9), wraplength=200)

        title_label = ttk.Label(main_frame, text="LIBS Software", style="Title.TLabel")
        title_label.pack(pady=(10, 2))

        subtitle_label = ttk.Label(
            main_frame,
            text="Select a mode to get started",
            style="Subtitle.TLabel"
        )
        subtitle_label.pack(pady=(0, 30))

        # Buttons frame
        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.pack(fill=tk.X, expand=True)
        buttons_frame.columnconfigure(0, weight=1)
        buttons_frame.columnconfigure(1, weight=1)

        # --- Analysis Mode Card ---
        analysis_frame = ttk.LabelFrame(buttons_frame, text="", padding=20)
        analysis_frame.grid(row=0, column=0, padx=15, sticky="nsew")

        # Load icon
        try:
            analysis_icon = Image.open("Icons/search_icon.png").resize((48, 48), Image.LANCZOS)
            self._analysis_icon = ImageTk.PhotoImage(analysis_icon)
            ttk.Label(analysis_frame, image=self._analysis_icon).pack(pady=(5, 8))
        except Exception:
            pass

        ttk.Label(analysis_frame, text="Analysis Mode", style="ModeTitle.TLabel").pack(pady=(0, 5))
        ttk.Label(
            analysis_frame,
            text="Import, process, and analyze\nLIBS spectral data.\nElement identification\nand calibration curves.",
            style="ModeDesc.TLabel",
            justify="center"
        ).pack(pady=(0, 12))

        analysis_btn = ttk.Button(
            analysis_frame,
            text="Open Analysis",
            command=lambda: self._select_mode("Analysis"),
            width=18
        )
        analysis_btn.pack(pady=(0, 5))

        # --- Acquisition Mode Card ---
        acquisition_frame = ttk.LabelFrame(buttons_frame, text="", padding=20)
        acquisition_frame.grid(row=0, column=1, padx=15, sticky="nsew")

        try:
            acq_icon = Image.open("Icons/spectrum_icon.png").resize((48, 48), Image.LANCZOS)
            self._acq_icon = ImageTk.PhotoImage(acq_icon)
            ttk.Label(acquisition_frame, image=self._acq_icon).pack(pady=(5, 8))
        except Exception:
            pass

        ttk.Label(acquisition_frame, text="Acquisition Mode", style="ModeTitle.TLabel").pack(pady=(0, 5))
        ttk.Label(
            acquisition_frame,
            text="Connect to USB4000\nspectrometer. Live view,\nhardware trigger capture,\nand auto-save spectra.",
            style="ModeDesc.TLabel",
            justify="center"
        ).pack(pady=(0, 12))

        acquisition_btn = ttk.Button(
            acquisition_frame,
            text="Open Acquisition",
            command=lambda: self._select_mode("Acquisition"),
            width=18
        )
        acquisition_btn.pack(pady=(0, 5))

        # Version / footer
        ttk.Label(
            main_frame,
            text="LIBS Data Analysis Software",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(side=tk.BOTTOM, pady=(15, 0))

    def _select_mode(self, mode):
        """Store the selected mode and close the launcher."""
        self.selected_mode = mode
        self.root.destroy()

    def _on_close(self):
        """Handle window close — exit the application."""
        self.selected_mode = None
        self.root.destroy()

    def run(self):
        """Run the launcher and return the selected mode."""
        self.root.mainloop()
        return self.selected_mode
