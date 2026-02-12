# main.py - Contains the main application loop and imports necessary modules and files. This file is the entry point for the application. 
# It also sets the TCL_LIBRARY and SV_TTK_THEME environment variables when running as a compiled executable.

# Importing necessary modules
import sys
import os
import traceback

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


def main():
    """Main entry point — shows the mode launcher, then opens the selected mode."""
    from mode_launcher import ModeLauncher

    # Show the mode selection launcher
    launcher = ModeLauncher()
    selected_mode = launcher.run()

    if selected_mode is None:
        # User closed the launcher without selecting a mode
        sys.exit(0)

    if selected_mode == "Analysis":
        from libs_app import App
        app = App()
        app.run()

    elif selected_mode == "Acquisition":
        from acquisition_app import AcquisitionApp
        acq_app = AcquisitionApp()
        acq_app.run()

        # Check if there's data to hand off to Analysis mode
        handoff = acq_app.get_handoff_data()

        # Destroy the acquisition window now (after mainloop exited)
        # so the Tcl interpreter is freed before creating a new Tk root.
        try:
            acq_app.root.destroy()
        except Exception:
            pass
        if handoff is not None:
            from libs_app import App
            import pandas as pd

            app = App()

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


