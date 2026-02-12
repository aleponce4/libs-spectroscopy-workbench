# acquisition_graph.py - Live-updating Matplotlib canvas for the Acquisition Mode.
# Optimized for real-time display using set_ydata() + draw_idle() instead of full redraws.

import tkinter as tk
from tkinter import ttk
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends._backend_tk import NavigationToolbar2Tk
import numpy as np

# Consistent font with the analysis mode
plt.rcParams['font.family'] = 'DejaVu Sans'
plt.rcParams['font.size'] = 12


class CustomToolbar(NavigationToolbar2Tk):
    """Toolbar without the Save button (export is handled by the sidebar)."""
    toolitems = [t for t in NavigationToolbar2Tk.toolitems if t[0] != 'Save']


def on_resize(event, canvas):
    canvas.draw()


def create_acquisition_graph(parent_frame):
    """
    Create the Matplotlib figure and canvas for live spectrum display.
    
    Axis limits use sensible defaults (200–1000 nm, 0–65535 counts) that are
    updated dynamically when a spectrometer connects — see
    ``configure_graph_for_device()``.
    
    Args:
        parent_frame: The Tkinter frame to embed the graph into.
        
    Returns:
        tuple: (graph_frame, fig, ax, canvas, line)
            - graph_frame: The containing frame
            - fig: Matplotlib Figure
            - ax: Matplotlib Axes
            - canvas: FigureCanvasTkAgg instance
            - line: The Line2D object for fast updates via set_ydata()
    """
    graph_frame = tk.Frame(parent_frame)
    graph_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # Create the figure
    fig, ax = plt.subplots(figsize=(14, 8))
    fig.subplots_adjust(left=0.1)

    # Default axis setup — will be reconfigured by configure_graph_for_device()
    ax.set_xlim([200, 1000])
    ax.set_ylim([0, 65535])
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Intensity (counts)")
    ax.set_title("Live Spectrum")
    ax.grid(which='both', linestyle='--', linewidth=0.5)

    # Pre-create a line with placeholder data for fast updates
    x_placeholder = np.linspace(200, 1000, 2048)
    y_placeholder = np.zeros_like(x_placeholder)
    line, = ax.plot(x_placeholder, y_placeholder, color='#0078D4', linewidth=0.8)

    # Embed in Tkinter
    canvas = FigureCanvasTkAgg(fig, master=graph_frame)
    canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 10))

    # Toolbar
    toolbar = CustomToolbar(canvas, graph_frame, pack_toolbar=False)
    toolbar.update()
    toolbar.pack(side=tk.TOP, anchor=tk.E, padx=(1, 40))

    # Resize handler
    canvas.mpl_connect('resize_event', lambda event: on_resize(event, canvas))

    return graph_frame, fig, ax, canvas, line


def configure_graph_for_device(ax, canvas, line, capabilities):
    """
    Reconfigure axis limits and placeholder data after a spectrometer connects.
    
    Args:
        ax: Matplotlib Axes
        canvas: FigureCanvasTkAgg
        line: The Line2D object created by create_acquisition_graph
        capabilities: A DeviceCapabilities instance from the spectrometer.
    """
    wl_min = capabilities.wavelength_min
    wl_max = capabilities.wavelength_max
    max_int = capabilities.max_intensity
    pixels = capabilities.pixel_count

    ax.set_xlim([wl_min, wl_max])
    ax.set_ylim([0, max_int])

    # Reset the line data to match the new pixel count
    x_placeholder = np.linspace(wl_min, wl_max, pixels)
    line.set_xdata(x_placeholder)
    line.set_ydata(np.zeros(pixels))

    ax.set_title(f"Live Spectrum — {capabilities.model}")
    canvas.draw_idle()


def update_spectrum_fast(ax, canvas, line, wavelengths, intensities):
    """
    Fast spectrum update — changes only the line data without clearing the axes.
    Y axis stays at the device's max intensity (set by configure_graph_for_device)
    to avoid constant rescaling.
    
    Args:
        ax: Matplotlib Axes
        canvas: FigureCanvasTkAgg
        line: The Line2D object created by create_acquisition_graph
        wavelengths: np.ndarray of wavelength values
        intensities: np.ndarray of intensity values
    """
    line.set_xdata(wavelengths)
    line.set_ydata(intensities)

    # Update X limits to match actual data range
    if len(wavelengths) > 0:
        ax.set_xlim([wavelengths[0], wavelengths[-1]])

    canvas.draw_idle()


def update_title(ax, canvas, title_text):
    """Update the plot title."""
    ax.set_title(title_text)
    canvas.draw_idle()


def highlight_captured_spectrum(ax, canvas, wavelengths, intensities, shot_index):
    """
    Briefly show the captured spectrum with a highlight effect.
    Used after a triggered capture to visually confirm the shot.
    """
    # Stash the current title so clear_highlight() can restore it
    ax._pre_capture_title = ax.get_title()

    # Flash the line in a different color
    highlight_line, = ax.plot(wavelengths, intensities, color='#FF4444', linewidth=1.2, alpha=0.8)
    ax.set_title(f"Captured — Shot #{shot_index}")
    canvas.draw_idle()
    return highlight_line


def clear_highlight(ax, canvas, highlight_line):
    """
    Remove the capture highlight line and restore the previous title.
    """
    try:
        highlight_line.remove()
    except (ValueError, AttributeError):
        pass
    # Restore title saved by highlight_captured_spectrum()
    ax.set_title(getattr(ax, '_pre_capture_title', 'Live Spectrum'))
    canvas.draw_idle()
