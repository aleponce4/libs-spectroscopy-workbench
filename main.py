# main.py - Contains the main application loop and imports necessary modules and files. This file is the entry point for the application. 
# It also sets the TCL_LIBRARY and SV_TTK_THEME environment variables when running as a compiled executable.

# Importing necessary modules
import sys
import os
import json
import traceback
import logging

# Configure logging so spectrometer/acquisition messages are visible
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

def log_error_to_file(error_message, file_name="error_log.txt"):
    with open(file_name, "a") as error_file:
        error_file.write(f"{error_message}\n")

def global_exception_handler(type, value, tb):
    error_message = "".join(traceback.format_exception(type, value, tb))
    log_error_to_file(error_message)
    print("An error occurred. Please check the error log file.")

sys.excepthook = global_exception_handler

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.chdir(sys._MEIPASS)

if getattr(sys, 'frozen', False):
    # Running as a compiled executable
    application_path = os.path.dirname(sys.executable)

    # In --onefile mode, data files are extracted to sys._MEIPASS (a temp dir),
    # NOT next to the exe. Point Tcl/Tk there so init.tcl is found.
    base_path = getattr(sys, '_MEIPASS', application_path)
    os.environ['TCL_LIBRARY'] = os.path.join(base_path, 'lib', 'tcl8.6')
    os.environ['TK_LIBRARY']  = os.path.join(base_path, 'lib', 'tk8.6')

    # Set the environment variable for the SV_TTK_THEME
    os.environ['SV_TTK_THEME'] = os.path.join(application_path, 'sv_ttk', 'theme')


if len(sys.argv) >= 3 and sys.argv[1] == "--seabreeze-probe":
    from spectrometer import _collect_seabreeze_probe
    print(json.dumps(_collect_seabreeze_probe(sys.argv[2])))
    sys.exit(0)


def main():
    """Main entry point — shows the mode launcher, then opens the selected mode."""
    import platform

    # DPI awareness (set once, before any window is created)
    if platform.system() == 'Windows':
        from ctypes import windll  # type: ignore
        windll.shcore.SetProcessDpiAwareness(1)

    # Create a SINGLE ThemedTk root for the entire application lifetime.
    # On Windows, destroying a Tk root and creating another one causes
    # the Tcl interpreter to hang, so we reuse one root throughout.
    from ttkthemes import ThemedTk
    import sv_ttk

    root = ThemedTk(theme="sun-valley")
    sv_ttk.set_theme("light")
    root.withdraw()  # Hidden while the launcher dialog is shown

    from mode_launcher import ModeLauncher

    # Show the mode selection launcher (as a Toplevel dialog on root)
    launcher = ModeLauncher(root)
    selected_mode = launcher.run()

    if selected_mode is None:
        # User closed the launcher without selecting a mode
        root.destroy()
        sys.exit(0)

    if selected_mode == "Analysis":
        from libs_app import App
        app = App(root)
        app.run()

    elif selected_mode == "Acquisition":
        from acquisition_app import AcquisitionApp
        acq_app = AcquisitionApp(root)
        acq_app.run()

        # Check if there's data to hand off to Analysis mode
        handoff = acq_app.get_handoff_data()

        if handoff is not None:
            from libs_app import App
            import pandas as pd

            # Clear existing widgets from the root before reusing it
            for widget in root.winfo_children():
                widget.destroy()

            app = App(root)

            # Load the captured spectrum directly into the analysis app
            app.x_data = pd.Series(handoff["wavelengths"])
            app.y_data = pd.Series(handoff["intensities"])
            app.ax.clear()
            app.ax.plot(app.x_data, app.y_data)
            app.line = app.ax.lines[-1]
            app.ax.set_xlim([app.x_data.min(), app.x_data.max()])
            app.ax.set_xlabel("Wavelength (nm)")
            app.ax.set_ylabel("Relative Intensity")
            app.ax.set_title("Acquired Spectrum")
            app.ax.grid(which='both', linestyle='--', linewidth=0.5)
            app.canvas.draw()

            app.run()


if __name__ == "__main__":
    main()


