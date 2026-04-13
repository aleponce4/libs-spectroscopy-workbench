# LIBS-Data-Analysis

A standalone Windows application for Laser Induced Breakdown Spectroscopy (LIBS) data analysis. This tool allows users to import, adjust, and analyze LIBS data, offering functionalities such as spectrum adjustment, plotting, and elemental line identification using a built-in periodic table.

## Modes

The application launches with a mode selector:

### Analysis Mode
- **Import Data**: Load LIBS spectral data files.
- **Adjust Spectrum**: Normalize and smooth (Moving Average, Gaussian, Savitzky-Golay, Median, Wavelet).
- **Adjust Plot**: Customize plot appearance and axis settings.
- **Search Element**: Periodic table interface for elemental line identification.
- **Export**: Save plots and processed data.

### Acquisition Mode
- **Spectrometer Control**: Connect to an Ocean Optics spectrometer via `python-seabreeze`.
  On Windows, the app can use either a WinUSB-bound device with `cseabreeze` or a
  libusb/libusbK-bound device with `pyseabreeze`. For source installs, make sure
  `libusb-package` is installed so `pyseabreeze` can load a `pyusb` backend.
- **Live View**: Real-time spectrum display with configurable integration time and averaging.
- **Hardware Trigger**: Arm external edge trigger for laser-synchronized capture.
- **Auto-Save**: Automatic saving of triggered spectra with sample naming and shot counter.
- **Send to Analysis**: Hand off captured spectra directly to Analysis Mode (no disk I/O).
- **Simulation Mode**: Built-in simulated spectrometer for testing without hardware.
- **Benchmark Runner**: `acquisition_benchmark.py` measures trigger/read/save timing and can
  report GUI queue latency when run through the acquisition window.

Example:
```bash
python acquisition_benchmark.py --simulate --mode test --shots 25 --auto-save
```

**Note**
python-seabreeze requires the spectrometer to use a WinUSB/libusb compatible driver rather than the default Ocean Optics driver.
If the device is not detected:
1 Install Zadig  
2 Connect spectrometer  
3 Select device from list  
4 Replace driver with WinUSB  
5 Restart software  
Note: OceanView will not work while WinUSB is installed. Reinstall Ocean Optics drivers to restore vendor software.****

## Installation

### Compiled Software

The software is provided as a standalone executable for Windows. No installation or prerequisites are required.

**[📥 Download the latest version here](https://github.com/aleponce4/LIBS-Data-Analysis/releases/latest)**

*Always get the latest version with bug fixes and new features from our GitHub releases.*

### From Source

1. **Clone the repository**.
2. **Install the required libraries**:
   ```
   pip install -r requirements.txt
   ```
3. **Run the application**:
   ```
   python main.py
   ```

## Usage

1. **Run the Software**: Double-click the executable, or run `python main.py` from source.
2. **Choose a Mode**: Select Analysis or Acquisition from the launcher.
![Alt text](images/1.png)
3. **Adjust and Analyze**: 

   - **3.1 Visual Apearance**: Use the 'Adjust Plot' tab to change the plot's visual apearance
   ![Alt text](images/2.png)
   - **3.2 Normalization**: Use the 'Adjust Spectrum' tab to apply apply normalization fitlers
   - **3.3 Peak Search**:  Use the 'Search Element' tab to search desired peaks. Select elements of interes by cliking in the periodic table, and select wanted ionization levels and peak database.
   ![Alt text](images/4.png)
   - **3.4 Final adjustments**:  Use the next tab to define an intensity treshold for peaks of itnerest and hide unlabel peaks.
   ![Alt text](images/5.png)

4. **Export Results**: Once satisfied, you can export your plots and adjusted data.

## Contributing

This project is currently in active development. If you have suggestions, feedback, or would like to contribute, please [contact the author](https://github.com/aleponce4)

## Acknowledgments

I'd like to extend my gratitude to:

- **Onteko Inc.** for providing the resources that made the development of this software possible.
- **Teresa Flores, Phd** for her invaluable spectroscopy expertise, guidance throughout the project, and for conducting the essential experimentation that led to the development of the elemental databases.
