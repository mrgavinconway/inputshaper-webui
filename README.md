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

Open `http://CONTAINER_IP:80`. The generated app password is in `/etc/inputshaper.env`. The Proxmox account/password is not stored in this application. Access is intended for a trusted LAN; use an HTTPS reverse proxy for access across untrusted networks.

## Import and investigate

Enable `keep_raw_data: True` in the printer's `[shaketune]` configuration, then upload the resulting input-shaper `.stdata`. Enter the original test's square corner velocity and smoothing limit; the data file may not contain those settings. Blank smoothing uses the pinned engine default. Add printer and conditions to make comparisons interpretable.

Select the measurement separately from X/Y/Z vibration components. Click a filter row to change the predicted residual; its checkbox toggles the dotted response on a labelled secondary axis. Region thresholds remain fixed to the original analysis when traces are hidden. Click a peak or the spectrogram to attach a frequency note; supply both frequency fields for a band. Plot choices and zoom save automatically per run.

You can select both X and Y `.stdata` files in one import. They are stored as a single run with shared test settings, and each file's measurements remain separate in the measurement selector. Filenames identify the source of each result; notes are attached to the selected measurement. Original files download together as a ZIP for paired imports. Single-file imports and previously saved runs remain supported. The 64 MiB compressed limit applies to each file; at most two files and 12 total measurements are accepted per import.

Leave the title blank to use the first valid `YYYYMMDD_HHMMSS` timestamp in the selected filenames (recorded as written, without timezone conversion). Files without that timestamp use their filenames as a descriptive fallback. **Delete import** removes a completed or failed import from the library; **Deleted imports** lets you restore it with all files, results, notes and plot choices intact. Deletion is recoverable and does not reclaim disk space.

Only modern Zstandard-compressed JSON `.stdata` is accepted. Legacy pickle inputs are deliberately rejected. The current importer is for input-shaper tests, not belt or vibration-profile analysis. Up to 12 measurements, 64 MiB compressed and 256 MiB decompressed per upload; one analysis process runs at a time, with a 10-minute timeout. Spectrograms are reduced to at most 500×500 display cells; spectral/filter results are retained at full computed resolution.

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
