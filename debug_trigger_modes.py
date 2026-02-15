"""
Debug script — run on the computer with the spectrometer connected.
Prints everything seabreeze knows about trigger modes for the USB4000.

Usage:
    python debug_trigger_modes.py

Copy/paste the full output and send it back.
"""

import sys

print("=== Trigger Mode Debug ===\n")
print(f"Python: {sys.version}")

# ── 1. Import seabreeze ────────────────────────────────────────────
try:
    import seabreeze
    print(f"seabreeze version: {seabreeze.__version__}")
except Exception as e:
    print(f"ERROR importing seabreeze: {e}")
    sys.exit(1)

try:
    seabreeze.use_backend("cseabreeze")
    print("Backend: cseabreeze")
except Exception:
    try:
        seabreeze.use_backend("pyseabreeze")
        print("Backend: pyseabreeze")
    except Exception as e2:
        print(f"Backend fallback failed: {e2}")

from seabreeze.spectrometers import list_devices, Spectrometer

# ── 2. List devices ───────────────────────────────────────────────
devices = list_devices()
print(f"\nDevices found: {len(devices)}")
for i, d in enumerate(devices):
    print(f"  [{i}] {d}")

if not devices:
    print("No devices — nothing to inspect.")
    sys.exit(0)

# ── 3. Open first device ──────────────────────────────────────────
spec = Spectrometer(devices[0])
print(f"\nOpened: model={spec.model}  serial={spec.serial_number}")
print(f"Pixels: {spec.pixels}")
print(f"Wavelength range: {spec.wavelengths()[0]:.1f} – {spec.wavelengths()[-1]:.1f} nm")

# ── 4. Inspect trigger modes — Approach 1 (feature instance) ─────
print("\n--- Approach 1: spec._dev.features['spectrometer'] ---")
try:
    spec_features = spec._dev.features.get("spectrometer", [])
    print(f"  spectrometer features list: {spec_features}")
    for j, feat in enumerate(spec_features):
        print(f"  feat[{j}] type: {type(feat).__name__}")
        print(f"    dir: {[a for a in dir(feat) if 'trigger' in a.lower() or 'mode' in a.lower()]}")
        if hasattr(feat, '_trigger_modes'):
            raw = feat._trigger_modes
            print(f"    _trigger_modes = {raw}")
            print(f"    type = {type(raw)}")
            for m in raw:
                print(f"      value={int(m)}  name={m.name if hasattr(m, 'name') else '???'}  "
                      f"type={type(m).__name__}  repr={repr(m)}")
        else:
            print("    NO _trigger_modes attribute")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 5. Inspect trigger modes — Approach 2 (device class) ─────────
print("\n--- Approach 2: device class _feature_classes ---")
try:
    dev_cls = type(spec._dev)
    print(f"  Device class: {dev_cls.__name__}  (module: {dev_cls.__module__})")
    if hasattr(dev_cls, '_feature_classes'):
        fc = dev_cls._feature_classes
        print(f"  _feature_classes keys: {list(fc.keys())}")
        spec_fcs = fc.get("spectrometer", [])
        print(f"  spectrometer feature classes: {spec_fcs}")
        for j, feat_cls in enumerate(spec_fcs):
            print(f"    [{j}] {feat_cls}")
            if hasattr(feat_cls, '_trigger_modes'):
                raw = feat_cls._trigger_modes
                print(f"      _trigger_modes = {raw}")
                print(f"      type = {type(raw)}")
                for m in raw:
                    print(f"        value={int(m)}  name={m.name if hasattr(m, 'name') else '???'}  "
                          f"type={type(m).__name__}  repr={repr(m)}")
            else:
                print(f"      NO _trigger_modes attribute")
    else:
        print("  NO _feature_classes on device class")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 6. Brute-force: look at all features ─────────────────────────
print("\n--- All features on spec._dev ---")
try:
    all_feats = spec._dev.features
    for feat_name, feat_list in all_feats.items():
        print(f"  {feat_name}: {feat_list}")
        for feat in feat_list:
            attrs = [a for a in dir(feat) if 'trigger' in a.lower() or 'mode' in a.lower()]
            if attrs:
                print(f"    trigger/mode attrs: {attrs}")
                for a in attrs:
                    try:
                        val = getattr(feat, a)
                        print(f"      {a} = {val}  (type={type(val).__name__})")
                    except Exception as ex:
                        print(f"      {a} -> ERROR: {ex}")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 7. Try TriggerMode enum directly ─────────────────────────────
print("\n--- TriggerMode enum (if available) ---")
for path in [
    "seabreeze.cseabreeze._wrapper.TriggerMode",
    "seabreeze.pyseabreeze.features.spectrometer.TriggerMode",
    "seabreeze.types.TriggerMode",
]:
    parts = path.rsplit(".", 1)
    try:
        mod = __import__(parts[0], fromlist=[parts[1]])
        cls = getattr(mod, parts[1])
        print(f"  {path}:")
        for member in cls:
            print(f"    {member.name} = {member.value}")
    except Exception:
        print(f"  {path}: not found")

# ── 8. Current trigger mode on the device ─────────────────────────
print("\n--- Current trigger mode ---")
try:
    print(f"  spec.trigger_mode() = {spec.trigger_mode()}")
except AttributeError:
    print("  spec.trigger_mode() -> AttributeError (not available as method)")
except Exception as e:
    print(f"  spec.trigger_mode() -> {e}")

# ── 9. Try setting trigger mode 3 (USB4000 edge trigger) ─────────
print("\n--- Test set_trigger_mode(3) ---")
try:
    spec.trigger_mode(3)
    print("  set_trigger_mode(3): OK (no error)")
except Exception as e:
    print(f"  set_trigger_mode(3): ERROR — {e}")

# Reset to normal
try:
    spec.trigger_mode(0)
except Exception:
    pass

# ── Done ──────────────────────────────────────────────────────────
print("\n--- Closing ---")
spec.close()
print("Done. Copy everything above and send it back.")
