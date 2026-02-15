# acquisition_sidebar.py - Sidebar controls for Acquisition Mode.
# Mirrors the visual style of the Analysis Mode sidebar (menu_functions.py).

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import functools


def create_acquisition_sidebar(app):
    """
    Build the sidebar for Acquisition Mode with spectrometer controls.
    
    Args:
        app: The AcquisitionApp instance.
    """
    app.sidebar_frame = ttk.Frame(app.root, width=280)
    app.sidebar_frame.place(x=0, y=0, width=280, relheight=1)

    # ─── Header ────────────────────────────────────────────────────────
    style = ttk.Style()
    style.configure("Emphasized.TLabel", font=("Segoe UI", 16, "bold"), foreground="black")
    style.configure("LeftAligned.TButton", anchor='w')
    style.configure("Status.TLabel", font=("Segoe UI", 9))
    style.configure("StatusValue.TLabel", font=("Segoe UI", 9, "bold"))

    header_label = ttk.Label(app.sidebar_frame, text="Acquisition", style="Emphasized.TLabel")
    header_label.grid(row=0, column=0, padx=10, pady=(60, 2))

    # ─── Connection Section ────────────────────────────────────────────
    conn_frame = ttk.LabelFrame(app.sidebar_frame, text="Spectrometer", padding=5)
    conn_frame.grid(row=1, column=0, padx=20, pady=(10, 5), sticky="ew")

    icon_size = (32, 32)

    # Connect button
    try:
        connect_icon = Image.open("Icons/Import_icon.png").resize(icon_size, Image.LANCZOS)
        app._connect_icon = ImageTk.PhotoImage(connect_icon)
    except Exception:
        app._connect_icon = None

    app.connect_btn = ttk.Button(
        conn_frame, text="Connect", image=app._connect_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_connect
    )
    app.connect_btn.grid(row=0, column=0, padx=5, pady=3, sticky="ew")

    # Disconnect button
    app.disconnect_btn = ttk.Button(
        conn_frame, text="Disconnect",
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_disconnect, state="disabled"
    )
    app.disconnect_btn.grid(row=1, column=0, padx=5, pady=3, sticky="ew")

    # Diagnose button
    app.diagnose_btn = ttk.Button(
        conn_frame, text="Diagnose…",
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_diagnose,
    )
    app.diagnose_btn.grid(row=2, column=0, padx=5, pady=3, sticky="ew")

    # Status indicator
    app.connection_status_var = tk.StringVar(value="Disconnected")
    status_label = ttk.Label(conn_frame, textvariable=app.connection_status_var,
                             style="Status.TLabel", foreground="gray")
    status_label.grid(row=3, column=0, padx=5, pady=(2, 5), sticky="w")

    # ─── Acquisition Controls ──────────────────────────────────────────
    acq_frame = ttk.LabelFrame(app.sidebar_frame, text="Acquisition", padding=5)
    acq_frame.grid(row=2, column=0, padx=20, pady=5, sticky="ew")

    # Live View button
    try:
        live_icon = Image.open("Icons/spectrum_icon.png").resize(icon_size, Image.LANCZOS)
        app._live_icon = ImageTk.PhotoImage(live_icon)
    except Exception:
        app._live_icon = None

    app.live_btn = ttk.Button(
        acq_frame, text="Live View", image=app._live_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_live_view, state="disabled"
    )
    app.live_btn.grid(row=0, column=0, padx=5, pady=3, sticky="ew")

    # Arm Trigger button
    try:
        arm_icon = Image.open("Icons/search_icon.png").resize(icon_size, Image.LANCZOS)
        app._arm_icon = ImageTk.PhotoImage(arm_icon)
    except Exception:
        app._arm_icon = None

    app.arm_btn = ttk.Button(
        acq_frame, text="Arm Trigger", image=app._arm_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_arm_trigger, state="disabled"
    )
    app.arm_btn.grid(row=1, column=0, padx=5, pady=3, sticky="ew")

    # Loop-arm toggle (continuous re-arm after each capture)
    app.loop_arm_var = tk.BooleanVar(value=False)
    app.loop_arm_check = ttk.Checkbutton(
        acq_frame, text="Loop (continuous arm)",
        variable=app.loop_arm_var,
    )
    app.loop_arm_check.grid(row=2, column=0, padx=20, pady=(0, 3), sticky="w")

    # Test Trigger button
    app.test_trigger_btn = ttk.Button(
        acq_frame, text="Test Trigger",
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_test_trigger, state="disabled"
    )
    app.test_trigger_btn.grid(row=3, column=0, padx=5, pady=3, sticky="ew")

    # Stop button
    try:
        stop_icon = Image.open("Icons/clean_icon.png").resize(icon_size, Image.LANCZOS)
        app._stop_icon = ImageTk.PhotoImage(stop_icon)
    except Exception:
        app._stop_icon = None

    app.stop_btn = ttk.Button(
        acq_frame, text="Stop", image=app._stop_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_stop, state="disabled"
    )
    app.stop_btn.grid(row=4, column=0, padx=5, pady=3, sticky="ew")

    # ─── Integration Time ──────────────────────────────────────────────
    int_frame = ttk.LabelFrame(app.sidebar_frame, text="Integration Time", padding=5)
    int_frame.grid(row=3, column=0, padx=20, pady=5, sticky="ew")

    app.integration_var = tk.StringVar(value="100")
    int_entry = ttk.Entry(int_frame, textvariable=app.integration_var, width=10)
    int_entry.grid(row=0, column=0, padx=5, pady=3, sticky="w")

    ttk.Label(int_frame, text="ms", style="Status.TLabel").grid(row=0, column=1, padx=2, pady=3, sticky="w")

    app.apply_int_btn = ttk.Button(
        int_frame, text="Apply", width=8,
        command=app.on_apply_integration, state="disabled"
    )
    app.apply_int_btn.grid(row=0, column=2, padx=5, pady=3, sticky="e")

    # Averages
    ttk.Label(int_frame, text="Averages:", style="Status.TLabel").grid(row=1, column=0, padx=5, pady=3, sticky="w")
    app.averages_var = tk.StringVar(value="1")
    avg_spinbox = ttk.Spinbox(int_frame, from_=1, to=100, width=5,
                               textvariable=app.averages_var, command=app.on_averages_changed)
    avg_spinbox.grid(row=1, column=1, columnspan=2, padx=5, pady=3, sticky="w")

    # Corrections
    app.correct_dark_var = tk.BooleanVar(value=False)
    app.dark_check = ttk.Checkbutton(int_frame, text="Dark count correction",
                                  variable=app.correct_dark_var,
                                  command=app.on_corrections_changed)
    app.dark_check.grid(row=2, column=0, columnspan=3, padx=5, pady=2, sticky="w")

    app.correct_nl_var = tk.BooleanVar(value=False)
    app.nl_check = ttk.Checkbutton(int_frame, text="Nonlinearity correction",
                                variable=app.correct_nl_var,
                                command=app.on_corrections_changed)
    app.nl_check.grid(row=3, column=0, columnspan=3, padx=5, pady=2, sticky="w")

    # Integration time range hint (populated after connection)
    app.int_range_var = tk.StringVar(value="")
    app.int_range_label = ttk.Label(int_frame, textvariable=app.int_range_var,
                                     style="Status.TLabel", foreground="gray")
    app.int_range_label.grid(row=4, column=0, columnspan=3, padx=5, pady=(0, 3), sticky="w")

    # ─── Auto-Save Settings ────────────────────────────────────────────
    save_frame = ttk.LabelFrame(app.sidebar_frame, text="Auto-Save", padding=5)
    save_frame.grid(row=4, column=0, padx=20, pady=5, sticky="ew")

    # Auto-save toggle
    app.auto_save_var = tk.BooleanVar(value=True)
    auto_save_check = ttk.Checkbutton(save_frame, text="Auto-save on trigger",
                                       variable=app.auto_save_var,
                                       command=app.on_auto_save_toggle)
    auto_save_check.grid(row=0, column=0, columnspan=2, padx=5, pady=3, sticky="w")

    # Sample name
    ttk.Label(save_frame, text="Sample Name:", style="Status.TLabel").grid(row=1, column=0, padx=5, pady=3, sticky="w")
    app.sample_name_var = tk.StringVar(value="Sample")
    sample_entry = ttk.Entry(save_frame, textvariable=app.sample_name_var, width=15)
    sample_entry.grid(row=1, column=1, padx=5, pady=3, sticky="ew")

    # Bind sample name change to reset shot index
    app.sample_name_var.trace_add("write", lambda *_: app.on_sample_name_changed())

    # Save directory
    ttk.Label(save_frame, text="Save to:", style="Status.TLabel").grid(row=2, column=0, padx=5, pady=3, sticky="w")
    app.save_dir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "LIBS_Data"))

    dir_btn = ttk.Button(save_frame, text="Browse...", width=10,
                          command=app.on_browse_save_dir)
    dir_btn.grid(row=2, column=1, padx=5, pady=3, sticky="ew")

    # Shot counter
    app.shot_count_var = tk.StringVar(value="Shots: 0")
    ttk.Label(save_frame, textvariable=app.shot_count_var,
              style="StatusValue.TLabel").grid(row=3, column=0, columnspan=2, padx=5, pady=3, sticky="w")

    # ─── Actions ───────────────────────────────────────────────────────
    action_frame = ttk.LabelFrame(app.sidebar_frame, text="Actions", padding=5)
    action_frame.grid(row=5, column=0, padx=20, pady=5, sticky="ew")

    # Send to Analysis
    try:
        send_icon = Image.open("Icons/export_icon.png").resize(icon_size, Image.LANCZOS)
        app._send_icon = ImageTk.PhotoImage(send_icon)
    except Exception:
        app._send_icon = None

    app.send_to_analysis_btn = ttk.Button(
        action_frame, text="Send to Analysis", image=app._send_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_send_to_analysis, state="disabled"
    )
    app.send_to_analysis_btn.grid(row=0, column=0, padx=5, pady=3, sticky="ew")

    # Save current spectrum
    try:
        save_icon = Image.open("Icons/savedata_icon.png").resize(icon_size, Image.LANCZOS)
        app._save_icon = ImageTk.PhotoImage(save_icon)
    except Exception:
        app._save_icon = None

    app.save_spectrum_btn = ttk.Button(
        action_frame, text="Save Spectrum", image=app._save_icon,
        compound='left', style="LeftAligned.TButton", width=18,
        command=app.on_save_spectrum, state="disabled"
    )
    app.save_spectrum_btn.grid(row=1, column=0, padx=5, pady=3, sticky="ew")

    # ─── Status Bar ────────────────────────────────────────────────────
    status_frame = ttk.Frame(app.sidebar_frame, padding=5)
    status_frame.grid(row=6, column=0, padx=20, pady=(10, 5), sticky="ew")

    app.worker_state_var = tk.StringVar(value="State: IDLE")
    ttk.Label(status_frame, textvariable=app.worker_state_var,
              style="StatusValue.TLabel").grid(row=0, column=0, padx=5, sticky="w")

    app.status_message_var = tk.StringVar(value="")
    ttk.Label(status_frame, textvariable=app.status_message_var,
              style="Status.TLabel", wraplength=220).grid(row=1, column=0, padx=5, pady=2, sticky="w")

    # ─── Logo ──────────────────────────────────────────────────────────
    try:
        logo = Image.open("Icons/Onteko_Logo.JPG")
        original_width, original_height = logo.size
        max_width = 200
        new_height = int((max_width / original_width) * original_height)
        logo_resized = logo.resize((max_width, new_height), Image.LANCZOS)
        app._logo_image = ImageTk.PhotoImage(logo_resized)
        logo_label = ttk.Label(app.sidebar_frame, image=app._logo_image)
        logo_label.grid(row=60, column=0, padx=(10, 1), pady=(20, 5))
    except Exception:
        pass
