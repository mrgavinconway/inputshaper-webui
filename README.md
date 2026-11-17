# Input Shaper Lab

A self-hosted investigation workspace for Klippain Shake&Tune input-shaper `.stdata` recordings. Runs in an unprivileged Debian 13 LXC.

## Features

- Durable original recordings, analysis results, notes, peak/band annotations and saved plot views.
- Independently selectable X+Y+Z, X, Y and Z traces; predicted residual for each selected shaper.
- Persistent Relax Region (5%) and Warning Region (12%) from the combined normalized PSD peak.
- ZV, MZV, EI, 2HUMP_EI and 3HUMP_EI response curves and filter table, including frequency, vibrations, smoothing and max acceleration.
- Linked spectrum/spectrogram inspection, absolute-scale before/after overlays, log scale, PNG and JSON export.
- Offline analysis using pinned, unmodified Shake&Tune and Klipper modules. No printer connection required.
- Automatic repository checks every 15 minutes; authenticated web-triggered install and automatic restart, isolated releases, preflight calculation test, health checks and rollback.

## Install in an LXC

Recommended starting allocation: 2 cores, 4 GB RAM, 16 GB disk, DHCP or a reserved address. Use Debian 13. Inside the container as root:

```sh
apt-get update && apt-get install -y git
git clone https://github.com/mrgavinconway/inputshaper-webui.git /tmp/inputshaper-install
bash /tmp/inputshaper-install/deploy/install.sh
```

Open `http://CONTAINER_IP`. The generated app password is in `/etc/inputshaper.env`. The Proxmox account/password is not stored in this application. Access is intended for a trusted LAN; use an HTTPS reverse proxy for access across untrusted networks.

## Import and investigate

Enable `keep_raw_data: True` in the printer's `[shaketune]` configuration, then upload the resulting input-shaper `.stdata`. Enter the original test's square corner velocity and smoothing limit; the data file may not contain those settings. Blank smoothing uses the pinned engine default. Add printer and conditions to make comparisons interpretable.

Select the measurement separately from X/Y/Z vibration components. Click a filter row to change the predicted residual; its checkbox toggles the dotted response on a labelled secondary axis. Region thresholds remain fixed to the original analysis when traces are hidden. Click a peak or the spectrogram to attach a frequency note; supply both frequency fields for a band. Plot choices and zoom save automatically per run.

You can select both X and Y `.stdata` files in one import. They are stored as a single run with shared test settings, and each file's measurements remain separate in the measurement selector. Filenames identify the source of each result; notes are attached to the selected measurement. Original files download together as a ZIP for paired imports. Single-file imports and previously saved runs remain supported. The 64 MiB compressed limit applies to each file; at most two files and 512 total measurements are accepted per import.

Leave the title blank to use the first valid `YYYYMMDD_HHMMSS` timestamp in the selected filenames (recorded as written, without timezone conversion). Files without that timestamp use their filenames as a descriptive fallback. **Delete import** removes a completed or failed import from the library; **Deleted imports** lets you restore it with all files, results, notes and plot choices intact. Deletion is recoverable and does not reclaim disk space.

Only modern Zstandard-compressed JSON `.stdata` is accepted. Legacy pickle inputs are rejected. Choose shaper, belts, static or vibrations analysis at import. Up to two files and 512 measurements (for bulk profiles), 64 MiB compressed and 256 MiB decompressed per file are accepted. One analysis process runs at a time, with a 10-minute timeout. Spectrograms use maximum-power pooling into at most 300×300 display cells, preserving brief/narrow features rather than skipping cells; metrics use full-resolution spectra.

Predicted residuals follow Shake&Tune's PSD-times-filter convention. They are estimates, not post-shaper measurements. A peak cannot identify a faulty component by itself; compare controlled changes and repeated baseline measurements. Region colours are relative heuristics, not machine safety limits.

## Updates and operations

The trusted upstream is this repository's `main` branch. Checks do not install updates unattended. **Updates → Install update & restart** prepares the new release, installs its pinned dependencies, runs a synthetic real-engine calculation, then switches the service and verifies its reported commit. Preparation failure leaves the running version intact. Activation failure restores the previous release and pre-update database backup. Non-fast-forward updates are rejected. Old releases remain available on disk for diagnosis.

The privileged updater is installed at `/usr/local/lib/inputshaper/update.py`. Normal app releases do not replace that root helper or service definitions; rerun the installer after reviewing changes to `deploy/`. The web service can invoke only the two fixed update/check units via sudo.

```sh
systemctl status inputshaper
journalctl -u inputshaper -u inputshaper-update
systemctl start inputshaper-check
```

Persistent data: `/var/lib/inputshaper` (SQLite library, originals, settings, computed JSON and processing logs). Back up this directory and `/etc/inputshaper.env`; use a stopped service or a SQLite-aware backup for consistency. Code is under `/opt/inputshaper/releases`, with `/opt/inputshaper/current` pointing at the active release.

To change the app password, edit `/etc/inputshaper.env` and restart `inputshaper`. This also invalidates existing sessions.

## Development and verification

Python 3.13 is recommended. Create a virtual environment and install `requirements.txt`; set `INPUTSHAPER_PASSWORD` and optionally `INPUTSHAPER_DATA`, then run `python -m uvicorn app.main:app --port 80`.

```sh
python -m app.selftest
pip install pytest httpx
python -m pytest tests
```

GPL-3.0; see `THIRD_PARTY.md` for analysis-engine revisions and attribution.


## Diagnostic workflow (analysis adapter v3)

- **Quality first:** sample rate, duration, frequency-bin spacing, timing deviation, gaps, possible clipping/quantisation and constant channels are reported. Sparse gaps up to three nominal periods, estimated missing samples at most 0.1%, and p99 timing deviation at most 20% retain spectra with an explicit review warning. Larger timing defects withhold spectra/shaper fitting. Original samples are used without silent resampling. Original data and an extrema-preserving waveform remain available. These are screening heuristics, not sensor certification. Damping is labelled as an upstream engine estimate, defaulted or unavailable; its normalised-spectrum convention is not a physical modal-damping measurement.
- **Confirm context:** record test mode, sensor/mount, mapping, position, excitation acceleration/rate, fans, temperatures and kinematics. Filename directions are labelled inferred. Test context allows individual direction confirmation for paired measurements. A global direction applies to every measurement.
- **Inspect signals:** ordinary acceleration PSD is available alongside calibration-normalised PSD. Residual/region overlays only apply to the normalised view. Per-axis spectrograms share an 80 dB colour range based on the combined map. Spectrum toggles retain a combined-power vertical scale. A native X/Y overlay appears automatically at the top of paired imports: exactly two combined-response traces on one ordinary-PSD scale. Its log scale and zoom are saved independently of the individual-measurement controls; no baseline selection is required.
- **Compare controlled changes:** baseline selection initially matches known direction. Quantitative comparisons block unknown/different direction, invalid timing, incompatible acquisition settings and different engine versions. Missing context remains an explicit caveat. Band power and RMS are calculated from ordinary PSD within the common measured range, without extrapolation.
- **Match peaks conservatively:** local maxima must exceed 1% of channel maximum and 5× median spectral level. Widths use half prominence above that median. Unique mutual candidates within 5 Hz are matched; ambiguous/unmatched peaks remain explicit. This is not proof of the same physical mode, and dominant-peak swaps are never treated automatically as frequency shifts.
- **Check repeats:** at least two distinct, compatible baselines produce an empirical min/max band-power range. Different recorded acquisition/experimental conditions are excluded. An outside-range observation is not statistical significance. Missing context remains unverified.
- **Investigate:** record a hypothesis, expected observation, controlled test, result, conclusion and status. Current/baseline/repeat references and a frequency band are linked. Each save captures engine versions, context, quality, peaks and available band powers as an evidence snapshot, preserved until the investigation is saved again.

Older imports can be upgraded with **Test context & re-analyse**, including expanding the maximum analysis frequency without importing another copy. Originals, measurement order and notes are retained. Successful prior results are archived in the run's `history` directory. Failed re-analysis retains the previous results/settings and reports the error. Re-analysis uses the app's pinned engine, not necessarily the printer's original version.

### Dedicated test views

**Belts:** exactly two A/B recordings for CoreXY or X/Z for CoreXZ, including a single original file containing both directions. Upstream belt curves, shape similarity and paired/unpaired peaks are displayed without equating the similarity score to a mechanical-health verdict or a belt-tension measurement.

**Static frequency:** ordinary spectra, per-axis time-frequency views and raw waveforms; no input-shaper fitting. Enter the excitation frequency as context.

**Vibration profiles:** measured speed/angle cells and integrated power, with click-through to each measured spectrum. Names must encode angle/speed, such as `an45_0sp100_0`. This is a measured-data view, not Shake&Tune's full interpolated polar/recommended-speed report. Unmeasured directions are not invented. Waveforms/spectrograms are omitted for bulk profile cells to bound output size.

The repeatability, fan and mounting templates propose controlled experiments. They do not assign a faulty component from frequency or from a Z-axis contribution alone. Acquisition and mounting differences can change measured amplitudes independently of any mechanical improvement.


## Archive history and observed changes

- Import any mix of `.zip`, `.tgz`, `.tar.gz` and `.stdata` files (128 recordings, 64 MiB each, 512 MiB cumulative expanded total). ZIP directories, archive metadata and expansion are bounded; encrypted ZIPs, links and traversal are rejected. Auto import separates X/Y shaper tests and belt comparisons using recording labels; ambiguous/incomplete belt pairs are rejected before creating any sets. Belt kinematics are inferred from A/B or X/Z labels and can be confirmed in test context. Different archive folders stay separate when pairing. Matching filename timestamps/prefixes and X/Y directions form pairs. Active identical source pairs are reused; deleted imports stay deleted.
- **Recording history** shows all groups in a relative-shape overview. Filter by excitation, recorded channel and inferred setup group. Grouping uses source labels, approximate sampling rate and apparent gravity direction, not a claim of identical mechanical conditions. Filename timestamps retain unknown timezone.
- Native-spectrum overlays, display-only interpolated heatmaps and exact native-PSD band integrations serve different purposes. Power/RMS and channel shares are not machine-health scores. Shape normalisation hides amplitude changes and is labelled explicitly.
- **What changed?** accepts explicit baseline/current recordings. It reports recorded setup changes, qualified band-power/RMS changes, conservative nearby peak matches and unmatched candidates. Compatible recordings receive a signed current-minus-baseline PSD plot on a common frequency grid; interpolation adds no resolution. Incompatible sampling, direction, engine or apparent orientation withholds numerical differences while retaining an exploratory overlay.
- Guided annotations preserve both recording identities, the selected band, observed metrics, warnings and user-entered changes/expectations/next test. Exact sample fingerprints prevent renamed/recompressed duplicates from counting as independent repeats; different files still require unchanged experimental conditions.
- Persistent-frequency candidates are detected from the original combined time–frequency matrix, before display pooling. Median/mean power, local frequency contrast and temporal coverage screens distinguish persistent candidates from many moving-sweep ridges. Long dwell or nonuniform excitation may also persist; these are not automatic fan/bearing/motor diagnoses. No detected candidate does not prove absence. Use a wider analysis range (for example 500 Hz) to inspect high-frequency background beyond the usual 200 Hz view.
- Compact rebuildable summary files index history without loading full spectrograms. Originals and complete analyses remain authoritative; cache corruption or unwritable cache storage does not invalidate them.

The supplied 50-record study motivated sampling-screen, coordinate-change and persistent-feature checks. Its original files and private study report are not committed to this repository. Regression tests use deterministic synthetic signals; optional local corpus checks skip when original study files are absent.

## Friendly names and focused results

Use **Rename set** to add or replace a friendly name while retaining the source timestamp and original set label. Clearing the name restores that label. Names appear in the library, search, history and comparison choices; recording IDs, originals, notes, saved views and evidence references remain unchanged. New imports also treat the optional name as a friendly suffix.

Recommended shapers and estimated acceleration limits (mm/s²) appear together at the top, with both directions shown for X/Y pairs. Empty persistence screens and clean timing checks are hidden; actionable quality findings remain visible. Formal investigations are available in a collapsed **Saved investigations** section; ordinary notes and change annotations remain directly accessible.

## Managing imports and copying printer settings

**Manage imports** offers search, an inclusive recording-date cutoff, select-visible, and a review of the exact sets before bulk deletion or restoration. The cutoff uses the source filename date; unknown dates are excluded. For example, choose **15 November 2026** to select that day and earlier. Deletion retains original files, notes, saved views and linked evidence. Processing sets cannot be selected. Up to 256 sets can be changed in one atomic operation; stale selections cause no changes.

**Recommended printer.cfg settings** appears below the acceleration cards. Copy the generated `[input_shaper]` block and merge it into the printer’s existing section. Choose between recommended shapers independently for X and Y. Only unique, usable axis recommendations are exported; missing axes and unavailable damping estimates are reported and omitted. Damping estimates are optional, enabled initially; Klipper normally defaults to 0.1 ([configuration reference](https://www.klipper3d.org/Config_Reference.html#input_shaper)). This generates text for review and does not connect to or modify the printer.
