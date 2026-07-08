<!--
SPDX-FileCopyrightText: 2025 Sebastian Andersson <sebastian@bittr.nu>
SPDX-FileCopyrightText: 2026 BlueBell-XA

SPDX-License-Identifier: GPL-3.0-or-later
-->

# spoolman2klipper

`spoolman2klipper` is a small Moonraker client service that copies the active
Spoolman spool and filament metadata into Klipper macro variables.

It exists so other Klipper macros can synchronously read filament data such as
material, colour, recommended hotend temperature, bed temperature, diameter,
density, and remaining weight.

## How It Works

The service:

- connects to Moonraker over websocket
- listens for Moonraker `active_spool_set` notifications
- fetches the active spool from Spoolman
- writes a fixed set of fields into one Klipper holder macro with
  `SET_GCODE_VARIABLE`

The default holder macro is `SPOOLMAN`. Missing macros or missing variables are
skipped, so you can define only the variables you actually use.

## Klipper Macro

Add this holder macro to your Klipper config, or copy
`klipper-example-macros.cfg`.

```ini
[gcode_macro SPOOLMAN]
description: Holder macro populated by spoolman2klipper
variable_spool_id: -1
variable_filament_name: "''"
variable_material: "''"
variable_vendor: "''"
variable_color: "''"
variable_extruder_temp: 0
variable_bed_temp: 0
variable_diameter: 0.0
variable_density: 0.0
variable_remaining_weight: 0.0
variable_filament_weight: 0.0
gcode:
  {action_respond_info("Spoolman data holder macro")}
```

Other macros can then read the values:

```ini
[gcode_macro PRINT_START]
gcode:
  {% set spool = printer["gcode_macro SPOOLMAN"] %}
  {% if spool.extruder_temp > 0 %}
    M104 S{spool.extruder_temp}
  {% endif %}
  {% if spool.bed_temp > 0 %}
    M140 S{spool.bed_temp}
  {% endif %}
```

## Variables

- `spool_id`
- `filament_name`
- `material`
- `vendor`
- `color`
- `extruder_temp`
- `bed_temp`
- `diameter`
- `density`
- `remaining_weight`
- `filament_weight`

## Installation & Setup

To install `spoolman2klipper` on your Klipper-based 3D printer, follow these steps:

### 1. Clone the Repository
Log in to your printer's terminal (e.g. via SSH) as your standard user (usually `pi`, `mks`, `debian`, etc.) and run:

```sh
cd ~
git clone https://github.com/BlueBell-XA/spoolman2klipper.git
cd spoolman2klipper
```

### 2. Run the Installer
Make the installer script executable and run it:

```sh
chmod +x scripts/install-service.sh
./scripts/install-service.sh
```

> [!NOTE]
> Do NOT run the installer with `sudo` directly. The script runs user-level tasks (creating the Python virtual environment) as your standard user to prevent permission issues, and will request `sudo` access automatically when registering the systemd system service.

The installer script automatically handles:
- Creating the Python virtual environment (`venv`) and installing all dependencies.
- Generating, copying, and starting the `spoolman2klipper` systemd service.

To force a specific service user during installation, you can set the environment variable:

```sh
SPOOLMAN2KLIPPER_USER=mks ./scripts/install-service.sh
```

### 3. Configure Moonraker & spoolman2klipper

#### Moonraker Integration (`moonraker.conf`)
Open `moonraker.conf` (either through the web UI or within `~/printer_data/config/`) and add the update manager configuration block directly into your file so that you can view and apply updates via Mainsail/Fluidd:

```ini
# Moonraker update manager configuration block for spoolman2klipper.
# Paste this block directly into moonraker.conf to enable automatic updates in Mainsail/Fluidd.
[update_manager spoolman2klipper]
# Specifies the type of software repository (git_repo indicates a Git-cloned repository)
type: git_repo
# The release channel to pull updates from (dev allows development/bleeding edge updates)
channel: dev
# The directory path on the printer where this project is checked out
path: ~/spoolman2klipper
# The Python virtual environment directory path used to run the service
virtualenv: ~/spoolman2klipper/venv
# The remote Git repository URL from which updates are fetched
origin: https://github.com/BlueBell-XA/spoolman2klipper.git
# The primary Git branch to track for updates
primary_branch: main
# Path to the dependencies file relative to the repository path
requirements: requirements.txt
# Systemd service names that Moonraker should restart after applying updates
managed_services: spoolman2klipper
# Additional tags and description displayed in the Moonraker/Mainsail interface
info_tags:
    desc=spoolman2klipper
```

#### Service Configuration (Optional)
Out of the box, `spoolman2klipper` runs with default settings which connect to Moonraker and Spoolman instances running locally on standard ports:
- `moonraker_url`: `ws://localhost:7125/websocket`
- `spoolman_url`: `http://localhost:7912/api`
- `klipper_macro`: `SPOOLMAN`

If you need to customize any settings (for example, if you run Spoolman or Moonraker on a different port/host), you can place a configuration file directly into your printer's config folder. This makes it easily editable directly from the Mainsail/Fluidd web UI file manager!

During installation, the installer automatically copies the default configuration file `spoolman2klipper.cfg` to your Klipper configuration directory (e.g., `~/printer_data/config/spoolman2klipper.cfg`). It does this by checking standard locations, or searching for the directory containing `printer.cfg` or `moonraker.conf`.

If you need to manually copy it (for example, if you deleted it or are performing a manual setup):

```sh
cp ~/spoolman2klipper/spoolman2klipper.cfg ~/printer_data/config/spoolman2klipper.cfg
```

Once copied (or automatically placed), you can edit `spoolman2klipper.cfg` directly through Mainsail/Fluidd:

```toml
[spoolman2klipper]
# Moonraker websocket address. Do not end with a slash.
moonraker_url = "ws://localhost:7125/websocket"

# Spoolman API address. Do not end with a slash.
spoolman_url = "http://localhost:7912/api"

# Klipper holder macro name that receives SET_GCODE_VARIABLE updates.
klipper_macro = "SPOOLMAN"

# Query Moonraker for the current active spool when this service starts.
sync_on_connect = true

# Seconds to wait before reconnecting to Moonraker or Spoolman.
reconnect_delay = 2.0

# HTTP/websocket request timeout in seconds.
request_timeout = 5.0

# Optional dedicated service log file. Leave commented to auto-detect the
# Klipper log directory and write ~/printer_data/logs/spoolman2klipper.log.
# log_file = "~/printer_data/logs/spoolman2klipper.log"
```

After modifying the configuration, restart the service to apply changes:
```sh
sudo systemctl restart spoolman2klipper
```

### Logs

`spoolman2klipper` writes a dedicated timestamped log file to:

```text
~/printer_data/logs/spoolman2klipper.log
```

If your printer stores `klippy.log`, `moonraker.log`, or `crowsnest.log` in
another common log directory, the service writes `spoolman2klipper.log` beside
those files instead. You can force a specific path with the optional `log_file`
setting in `spoolman2klipper.cfg`.

The service also keeps writing to stderr for systemd, so older output remains
available with:

```sh
journalctl -u spoolman2klipper
```

### 4. Verify the Installation
Check that the system service is active and running correctly:

```sh
sudo systemctl status spoolman2klipper
```

---

## Uninstalling

If you need to completely remove the service and its files, run the uninstallation script:

```sh
chmod +x scripts/uninstall-service.sh
./scripts/uninstall-service.sh
```

The uninstallation script will:
- Stop, disable, and delete the `spoolman2klipper` systemd service.
- Delete the Python virtual environment (`venv`).

> [!NOTE]
> The uninstallation script automatically detects your Klipper configuration directory and removes the `spoolman2klipper.cfg` configuration file if it exists.

## Development

Create a venv and install dependencies:

```sh
python3 -m venv venv
venv/bin/pip3 install -r requirements-dev.txt
```

Run tests:

```sh
venv/bin/python -m pytest -q
```

Run lint:

```sh
make lint
```
