# Analysis engine provenance

The unmodified `vendor/shaketune` Python package is from Frix-x/klippain-shaketune commit `354ee4be6db218715239f7b9411c2c37fdea23dd` (GPL-3.0).

`vendor/extras/shaper_calibrate.py`, `shaper_defs.py`, `vendor/mathutil.py` and `vendor/queuelogger.py` are from Klipper3d/klipper commit `29d4ec2d6d840ee839333306003a400da186d41d` (GPL-3.0). Original copyright headers are retained. See LICENSE and vendor/KLIPPER-LICENSE.

Plotly.js is distributed under its MIT license, included in its bundled header.

The app calls Shake&Tune's ShaperComputation directly. Region boundaries, normalized PSDs, filter curves, damping and recommendations are produced by that engine. Displayed residual curves follow its plotting convention: normalized combined PSD multiplied by shaper vals. They are predictions, not new measurements.
