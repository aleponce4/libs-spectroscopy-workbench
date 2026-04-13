# acquisition_sidebar.py - Sidebar controls for Acquisition Mode.
# Mirrors the visual style of the Analysis Mode sidebar (menu_functions.py).

import os
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk


ACQUISITION_SIDEBAR_WIDTH = 360
_SIDEBAR_SECTION_PAD_X = 18
_SIDEBAR_TEXT_WRAP = ACQUISITION_SIDEBAR_WIDTH - (_SIDEBAR_SECTION_PAD_X * 2) - 28


def _install_mousewheel_scrolling(canvas: tk.Canvas, *widgets):
    """Scroll the sidebar while the pointer is over it."""
    hover_depth = 0

    def _on_mousewheel(event):
        if event.num == 4:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            canvas.yview_scroll(1, "units")
        elif event.delta:
            canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _bind(_event):
        nonlocal hover_depth
        hover_depth += 1
        if hover_depth == 1:
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind(_event):
        nonlocal hover_depth
        hover_depth = max(0, hover_depth - 1)

        def _maybe_unbind():
            if hover_depth == 0:
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")

        canvas.after_idle(_maybe_unbind)

    for widget in widgets:
        widget.bind("<Enter>", _bind, add="+")
        widget.bind("<Leave>", _unbind, add="+")


def create_acquisition_sidebar(app):
    """
    Build the sidebar for Acquisition Mode with spectrometer controls.

    Args:
        app: The AcquisitionApp instance.
    """
    style = ttk.Style()
    style.configure("Emphasized.TLabel", font=("Segoe UI", 16, "bold"), foreground="black")
    style.configure("LeftAligned.TButton", anchor="w")
    style.configure(
        "ArmReady.TButton",
        anchor="w",
        font=("Segoe UI", 9, "bold"),
        padding=(10, 6),
        foreground="#183247",
        background="#E8F0F7",
        bordercolor="#B7C8D8",
    )
    style.map(
        "ArmReady.TButton",
        background=[("active", "#DCEAF4"), ("pressed", "#D0E2EE")],
        foreground=[("active", "#102638"), ("pressed", "#102638")],
        bordercolor=[("active", "#9EB7CC"), ("pressed", "#8DA9BF")],
    )
    style.configure(
        "ArmArmed.TButton",
        anchor="w",
        font=("Segoe UI", 9, "bold"),
        padding=(10, 6),
        foreground="#143223",
        background="#DDF3E5",
        bordercolor="#7FBF96",
    )
    style.map(
        "ArmArmed.TButton",
        background=[("active", "#D7EFDF"), ("pressed", "#CCE8D6")],
        foreground=[("active", "#10281C"), ("pressed", "#10281C")],
        bordercolor=[("active", "#69AB81"), ("pressed", "#5D9A74")],
    )
    style.configure("Status.TLabel", font=("Segoe UI", 9))
    style.configure("StatusValue.TLabel", font=("Segoe UI", 9, "bold"))

    app.sidebar_outer = ttk.Frame(app.root)
    app.sidebar_outer.place(x=0, y=0, width=ACQUISITION_SIDEBAR_WIDTH, relheight=1)

    canvas_bg = app.root.cget("bg")
    app.sidebar_canvas = tk.Canvas(
        app.sidebar_outer,
        highlightthickness=0,
        borderwidth=0,
        bg=canvas_bg,
    )
    app.sidebar_scrollbar = ttk.Scrollbar(
        app.sidebar_outer,
        orient=tk.VERTICAL,
        command=app.sidebar_canvas.yview,
    )
    app.sidebar_canvas.configure(yscrollcommand=app.sidebar_scrollbar.set)
    app.sidebar_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    app.sidebar_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    app.sidebar_frame = ttk.Frame(app.sidebar_canvas)
    app.sidebar_frame.columnconfigure(0, weight=1)
    sidebar_window = app.sidebar_canvas.create_window((0, 0), window=app.sidebar_frame, anchor="nw")

    def _refresh_sidebar_scrollregion(_event=None):
        app.sidebar_canvas.configure(scrollregion=app.sidebar_canvas.bbox("all"))

    def _resize_sidebar_content(event):
        app.sidebar_canvas.itemconfigure(sidebar_window, width=event.width)

    app.sidebar_frame.bind("<Configure>", _refresh_sidebar_scrollregion)
    app.sidebar_canvas.bind("<Configure>", _resize_sidebar_content)
    _install_mousewheel_scrolling(app.sidebar_canvas, app.sidebar_outer, app.sidebar_canvas, app.sidebar_frame)
    app._refresh_sidebar_scrollregion = _refresh_sidebar_scrollregion

    icon_size = (32, 32)

    conn_frame = ttk.LabelFrame(app.sidebar_frame, text="Spectrometer", padding=6)
    conn_frame.grid(row=0, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=(14, 6), sticky="ew")
    conn_frame.columnconfigure(0, weight=1)

    try:
        connect_icon = Image.open("Icons/Import_icon.png").resize(icon_size, Image.LANCZOS)
        app._connect_icon = ImageTk.PhotoImage(connect_icon)
    except Exception:
        app._connect_icon = None

    app.connect_btn = ttk.Button(
        conn_frame,
        text="Connect",
        image=app._connect_icon,
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_connect,
    )
    app.connect_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    app.disconnect_btn = ttk.Button(
        conn_frame,
        text="Disconnect",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_disconnect,
        state="disabled",
    )
    app.disconnect_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    app.diagnose_btn = ttk.Button(
        conn_frame,
        text="Diagnose...",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_diagnose,
    )
    app.diagnose_btn.grid(row=2, column=0, padx=6, pady=4, sticky="ew")

    app.connection_status_var = tk.StringVar(value="Disconnected")
    status_label = ttk.Label(
        conn_frame,
        textvariable=app.connection_status_var,
        style="Status.TLabel",
        foreground="gray",
        wraplength=_SIDEBAR_TEXT_WRAP,
    )
    status_label.grid(row=3, column=0, padx=6, pady=(2, 6), sticky="ew")

    acq_frame = ttk.LabelFrame(app.sidebar_frame, text="Acquisition", padding=6)
    acq_frame.grid(row=1, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    acq_frame.columnconfigure(0, weight=1)

    try:
        live_icon = Image.open("Icons/spectrum_icon.png").resize(icon_size, Image.LANCZOS)
        app._live_icon = ImageTk.PhotoImage(live_icon)
    except Exception:
        app._live_icon = None

    app.live_btn = ttk.Button(
        acq_frame,
        text="Live View",
        image=app._live_icon,
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_live_view,
        state="disabled",
    )
    app.live_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    try:
        arm_icon = Image.open("Icons/trigger_icon.png").resize(icon_size, Image.LANCZOS)
        app._arm_icon = ImageTk.PhotoImage(arm_icon)
    except Exception:
        app._arm_icon = None

    app.arm_btn = ttk.Button(
        acq_frame,
        text="Arm Trigger",
        image=app._arm_icon,
        compound="left",
        style="ArmReady.TButton",
        command=app.on_arm_trigger,
        state="disabled",
    )
    app.arm_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    app.arm_status_var = tk.StringVar(value="Trigger unavailable")
    app.arm_status_chip = tk.Label(
        acq_frame,
        textvariable=app.arm_status_var,
        bg="#E4E8ED",
        fg="#4D5A67",
        font=("Segoe UI", 8, "bold"),
        padx=10,
        pady=4,
        anchor="w",
    )
    app.arm_status_chip.grid(row=2, column=0, padx=6, pady=(0, 4), sticky="ew")

    app.test_trigger_btn = ttk.Button(
        acq_frame,
        text="Test Trigger",
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_test_trigger,
        state="disabled",
    )
    app.test_trigger_btn.grid(row=3, column=0, padx=6, pady=4, sticky="ew")

    try:
        stop_icon = Image.open("Icons/clean_icon.png").resize(icon_size, Image.LANCZOS)
        app._stop_icon = ImageTk.PhotoImage(stop_icon)
    except Exception:
        app._stop_icon = None

    app.stop_btn = ttk.Button(
        acq_frame,
        text="Stop",
        image=app._stop_icon,
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_stop,
        state="disabled",
    )
    app.stop_btn.grid(row=4, column=0, padx=6, pady=4, sticky="ew")

    int_frame = ttk.LabelFrame(app.sidebar_frame, text="Advanced Options", padding=6)
    int_frame.grid(row=2, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    int_frame.columnconfigure(0, weight=1)

    app.integration_var = tk.StringVar(value="100")
    app.advanced_options_expanded = tk.BooleanVar(value=False)
    app.advanced_options_label_var = tk.StringVar()
    app.advanced_options_body = ttk.Frame(int_frame)
    app.advanced_options_body.columnconfigure(0, weight=1)

    def _set_advanced_options_label(*_):
        arrow = "v" if app.advanced_options_expanded.get() else ">"
        app.advanced_options_label_var.set(
            f"Integration: {app.integration_var.get()} ms {arrow}"
        )

    def _toggle_advanced_options():
        app.advanced_options_expanded.set(not app.advanced_options_expanded.get())
        if app.advanced_options_expanded.get():
            app.advanced_options_body.grid(row=1, column=0, sticky="ew")
        else:
            app.advanced_options_body.grid_remove()
        _set_advanced_options_label()
        app.root.after_idle(app._refresh_sidebar_scrollregion)

    app.integration_var.trace_add("write", _set_advanced_options_label)
    _set_advanced_options_label()

    app.advanced_options_btn = ttk.Button(
        int_frame,
        textvariable=app.advanced_options_label_var,
        command=_toggle_advanced_options,
        style="LeftAligned.TButton",
    )
    app.advanced_options_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    int_entry = ttk.Entry(app.advanced_options_body, textvariable=app.integration_var)
    int_entry.grid(row=0, column=0, padx=(6, 4), pady=4, sticky="ew")

    ttk.Label(app.advanced_options_body, text="ms", style="Status.TLabel").grid(
        row=0,
        column=1,
        padx=2,
        pady=4,
        sticky="w",
    )

    app.apply_int_btn = ttk.Button(
        app.advanced_options_body,
        text="Apply",
        command=app.on_apply_integration,
        state="disabled",
    )
    app.apply_int_btn.grid(row=0, column=2, padx=(6, 6), pady=4, sticky="e")

    ttk.Label(app.advanced_options_body, text="Averages:", style="Status.TLabel").grid(
        row=1,
        column=0,
        padx=6,
        pady=4,
        sticky="w",
    )
    app.averages_var = tk.StringVar(value="1")
    avg_spinbox = ttk.Spinbox(
        app.advanced_options_body,
        from_=1,
        to=100,
        width=6,
        textvariable=app.averages_var,
        command=app.on_averages_changed,
    )
    avg_spinbox.grid(row=1, column=1, columnspan=2, padx=(0, 6), pady=4, sticky="w")

    app.correct_dark_var = tk.BooleanVar(value=False)
    app.dark_check = ttk.Checkbutton(
        app.advanced_options_body,
        text="Dark count correction",
        variable=app.correct_dark_var,
        command=app.on_corrections_changed,
    )
    app.dark_check.grid(row=2, column=0, columnspan=3, padx=6, pady=2, sticky="w")

    app.correct_nl_var = tk.BooleanVar(value=False)
    app.nl_check = ttk.Checkbutton(
        app.advanced_options_body,
        text="Nonlinearity correction",
        variable=app.correct_nl_var,
        command=app.on_corrections_changed,
    )
    app.nl_check.grid(row=3, column=0, columnspan=3, padx=6, pady=2, sticky="w")

    app.int_range_var = tk.StringVar(value="")
    app.int_range_label = ttk.Label(
        app.advanced_options_body,
        textvariable=app.int_range_var,
        style="Status.TLabel",
        foreground="gray",
        wraplength=_SIDEBAR_TEXT_WRAP,
    )
    app.int_range_label.grid(row=4, column=0, columnspan=3, padx=6, pady=(0, 4), sticky="ew")
    app.advanced_options_body.grid(row=1, column=0, sticky="ew")
    app.advanced_options_body.grid_remove()

    save_frame = ttk.LabelFrame(app.sidebar_frame, text="Auto-Save", padding=6)
    save_frame.grid(row=3, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    save_frame.columnconfigure(0, weight=0)
    save_frame.columnconfigure(1, weight=1)

    app.auto_save_var = tk.BooleanVar(value=True)
    auto_save_check = ttk.Checkbutton(
        save_frame,
        text="Auto-save on trigger",
        variable=app.auto_save_var,
        command=app.on_auto_save_toggle,
    )
    auto_save_check.grid(row=0, column=0, columnspan=2, padx=6, pady=4, sticky="w")

    ttk.Label(save_frame, text="Experiment Name:", style="Status.TLabel").grid(
        row=1,
        column=0,
        padx=6,
        pady=4,
        sticky="w",
    )
    app.sample_name_var = tk.StringVar(value="Sample")
    sample_entry = ttk.Entry(save_frame, textvariable=app.sample_name_var)
    sample_entry.grid(row=1, column=1, padx=6, pady=4, sticky="ew")
    app.sample_name_var.trace_add("write", lambda *_: app.on_sample_name_changed())

    ttk.Label(save_frame, text="Save to:", style="Status.TLabel").grid(
        row=2,
        column=0,
        padx=6,
        pady=4,
        sticky="w",
    )
    app.save_dir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "LIBS_Data"))

    dir_btn = ttk.Button(save_frame, text="Browse...", command=app.on_browse_save_dir)
    dir_btn.grid(row=2, column=1, padx=6, pady=4, sticky="ew")

    app.shot_count_var = tk.StringVar(value="Shots: 0")
    ttk.Label(
        save_frame,
        textvariable=app.shot_count_var,
        style="StatusValue.TLabel",
    ).grid(row=3, column=0, columnspan=2, padx=6, pady=4, sticky="w")

    app.plate_mode_var = tk.BooleanVar(value=False)
    plate_mode_check = ttk.Checkbutton(
        save_frame,
        text="High-throughput plate mode",
        variable=app.plate_mode_var,
        command=app.on_plate_mode_toggle,
    )
    plate_mode_check.grid(row=4, column=0, columnspan=2, padx=6, pady=(8, 4), sticky="w")

    app.configure_plate_btn = ttk.Button(
        save_frame,
        text="Configure Plate...",
        command=app.on_configure_plate,
        state="disabled",
    )
    app.configure_plate_btn.grid(row=5, column=0, columnspan=2, padx=6, pady=4, sticky="ew")

    app.plate_progress_var = tk.StringVar(value="")
    app.plate_progress_label = ttk.Label(
        save_frame,
        textvariable=app.plate_progress_var,
        style="Status.TLabel",
        wraplength=_SIDEBAR_TEXT_WRAP,
    )
    app.plate_progress_label.grid(row=6, column=0, columnspan=2, padx=6, pady=2, sticky="ew")

    plate_actions_frame = ttk.Frame(save_frame)
    plate_actions_frame.grid(row=7, column=0, columnspan=2, padx=6, pady=(4, 6), sticky="ew")
    plate_actions_frame.columnconfigure(0, weight=1)
    plate_actions_frame.columnconfigure(1, weight=1)

    app.discard_plate_shot_btn = ttk.Button(
        plate_actions_frame,
        text="Discard Shot",
        command=app.on_discard_last_plate_shot,
        state="disabled",
    )
    app.discard_plate_shot_btn.grid(row=0, column=0, padx=(0, 3), sticky="ew")

    app.finish_plate_btn = ttk.Button(
        plate_actions_frame,
        text="Finish Plate",
        command=app.on_finish_plate_early,
        state="disabled",
    )
    app.finish_plate_btn.grid(row=0, column=1, padx=(3, 0), sticky="ew")

    action_frame = ttk.LabelFrame(app.sidebar_frame, text="Actions", padding=6)
    action_frame.grid(row=4, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=6, sticky="ew")
    action_frame.columnconfigure(0, weight=1)

    try:
        send_icon = Image.open("Icons/export_icon.png").resize(icon_size, Image.LANCZOS)
        app._send_icon = ImageTk.PhotoImage(send_icon)
    except Exception:
        app._send_icon = None

    app.send_to_analysis_btn = ttk.Button(
        action_frame,
        text="Send to Analysis",
        image=app._send_icon,
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_send_to_analysis,
        state="disabled",
    )
    app.send_to_analysis_btn.grid(row=0, column=0, padx=6, pady=4, sticky="ew")

    try:
        save_icon = Image.open("Icons/savedata_icon.png").resize(icon_size, Image.LANCZOS)
        app._save_icon = ImageTk.PhotoImage(save_icon)
    except Exception:
        app._save_icon = None

    app.save_spectrum_btn = ttk.Button(
        action_frame,
        text="Save Spectrum",
        image=app._save_icon,
        compound="left",
        style="LeftAligned.TButton",
        command=app.on_save_spectrum,
        state="disabled",
    )
    app.save_spectrum_btn.grid(row=1, column=0, padx=6, pady=4, sticky="ew")

    status_frame = ttk.Frame(app.sidebar_frame, padding=6)
    status_frame.grid(row=5, column=0, padx=_SIDEBAR_SECTION_PAD_X, pady=(10, 12), sticky="ew")
    status_frame.columnconfigure(0, weight=1)

    app.worker_state_var = tk.StringVar(value="State: IDLE")
    ttk.Label(
        status_frame,
        textvariable=app.worker_state_var,
        style="StatusValue.TLabel",
    ).grid(row=0, column=0, padx=6, sticky="w")

    app.status_message_var = tk.StringVar(value="")
    ttk.Label(
        status_frame,
        textvariable=app.status_message_var,
        style="Status.TLabel",
        wraplength=_SIDEBAR_TEXT_WRAP,
    ).grid(row=1, column=0, padx=6, pady=2, sticky="ew")

    app.root.after_idle(app._refresh_sidebar_scrollregion)
