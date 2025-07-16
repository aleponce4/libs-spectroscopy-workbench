# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\element_database.csv', '.'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\persistent_lines.csv', '.'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\calibration_data_library.csv', '.'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\add_to_library_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\apply_library_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\clean_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\export_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\help_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\Import_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\main_icon.ico', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\main_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\plot_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\savedata_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\search_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\spectrum_icon.png', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\Onteko_Logo.jpg', 'Icons'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Help', 'Help'), ('C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\images', 'images')]
binaries = []
hiddenimports = ['ipaddress', 'urllib.parse', 'pathlib', 'email.mime.text', 'email.mime.multipart', 'email.mime.base', 'html.parser', 'http.client', 'http.server', 'PIL', 'PIL._tkinter_finder', 'markdown', 'matplotlib', 'matplotlib.backends.backend_tkagg', 'matplotlib.figure', 'numpy', 'numpy.core._methods', 'numpy.lib.format', 'pandas', 'pandas._libs.tslibs.base', 'pandas._libs.tslibs.nattype', 'pywt', 'scipy', 'scipy.sparse.csgraph._validation', 'scipy.special._ufuncs', 'sklearn', 'sklearn.neighbors.typedefs', 'sklearn.utils._cython_blas', 'statsmodels', 'sv_ttk', 'textalloc', 'tkhtmlview', 'ttkthemes', 'ttkthemes.themed_style', 'ttkthemes.themed_tk', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', 'matplotlib.backends._backend_tk', 'matplotlib.backends.backend_pdf', 'scipy.stats', 'scipy.optimize', 'scipy.interpolate', 'sklearn.ensemble', 'sklearn.tree', 'sklearn.linear_model', 'numpy.random', 'numpy.linalg', 'numpy.fft', 'pandas.io.formats.style', 'pandas.plotting', 'pkg_resources.py2_warn', 'pkg_resources', 'openpyxl', 'xlsxwriter', 'certifi', 'urllib3']
tmp_ret = collect_all('ttkthemes')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('sv_ttk')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LIBS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\alepo\\Desktop\\Onteko\\LIBS-Data-Analysis\\Icons\\main_icon.ico'],
)
