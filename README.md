<!--
SPDX-FileCopyrightText: 2025 Sebastian Andersson <sebastian@bittr.nu>

SPDX-License-Identifier: GPL-3.0-or-later
-->

[![REUSE status](https://api.reuse.software/badge/github.com/bofh69/spool2klipper)](https://api.reuse.software/info/github.com/bofh69/spool2klipper)
![GitHub Workflow Status](https://github.com/bofh69/spool2klipper/actions/workflows/pylint.yml/badge.svg)


# spool2klipper

A program that transfers info about Spoolman's data about the active Spool/Filament/Manufacturer to
[Klipper](https://www.klipper3d.org/).

The program is a [Moonraker](https://github.com/Arksine/moonraker) agent that listens for notifications
about changed spools. When the spool is changed, it asks spoolman about the changed spool's data
and it asks Moonraker to run special gcode macros in Klipper for storing data about the new spool.

## Prepare for running spool2klipper:

In the cloned repository's dir run:
```sh
virtualenv venv
venv/bin/pip3 install -r requirements.txt
```

<!-- Copy and update the `spool2klipper.cfg` to `~/.config/spool2klipper/spool2klipper.cfg`. -->

## Preparing Klipper

When spool data is to be sent to Klipper, spool2klipper looks for gcode macros with the name
`_SPOOLMAN_SET_FIELD_`_fieldname_. Ie:
`_SPOOLMAN_SET_FIELD_filament_id`

The macro will be called with the argument `VALUE=`_fields-value_.

If the active spool is cleared in Moonraker, this agent will call (if available):
`_SPOOLMAN_CLEAR_FIELDS`

After all the macros have been called, _SPOOLMAN_DONE will be called, if available
`_SPOOLMAN_DONE`

Add gcode macros to Klipper's config to receive and handle the fields you are interested in.

Here's a simple example:

```ini
[gcode_macro _SPOOLMAN_SET_FIELD_filament_id]
description: Store loaded filament's ID
gcode:
  {% if params.VALUE %}
    {% set id = params.VALUE|int %}
    SAVE_VARIABLE VARIABLE=active_filament_id VALUE={id}
    RESPOND MSG="Setting active_filament_id to {id}"
  {% else %}
    {action_respond_info("Parameter 'VALUE' is required")}
  {% endif %}

[gcode_macro _SPOOLMAN_CLEAR_FIELDS]
description: Removes spool info
gcode:
    SAVE_VARIABLE VARIABLE=active_filament_id VALUE=None
    RESPOND MSG="Clearing active_filament_id"

[gcode_macro _SPOOLMAN_DONE]
description: The data was transferred
gcode:
    RESPOND TYPE=command MSG="CHANGE FILAMENT"
  {% endif %}

```

## Run automaticly with systemd

Copy spool2klippper.service to `/etc/systemd/system`, then run:

```sh
sudo systemctl start spool2klipper
sudo systemctl enable spool2klipper
```

To see its status, run:
```sh
sudo systemctl status spool2klipper
```

## Automatic upgrades with Moonraker

Moonraker can be configured to help upgrade spool2klipper.

Copy the the `moonraker-spool2klipper.cfg` file to the same dir as where
`moonraker.conf` is. Include the config file by adding:
```toml
[include moonraker-spool2klipper.cfg]
```
to Moonraker's config file (moonraker.conf).

## See also

This program was made to make it easier to use [spoolman2slicer](https://github.com/bofh69/spoolman2slicer) when not using [nfc2klipper](https://github.com/bofh69/nfc2klipper).

## Developer info

Pull requests are happily accepted, but before making one make sure
the code is formatted with black and passes pylint without errors.

The code can be formatted by running `make fmt` and checked with pylint
with `make lint`.

If you add a new file, run "make reuse" to lint its licensing information.
