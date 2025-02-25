#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025 Sebastian Andersson <sebastian@bittr.nu>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Moonraker agent to send Spoolman's spool info to Klipper

It listens for active_spool_set events from moonraker,
that will cause it to lookup the new spool's data
and for every field, if there exists a gcode macro
with the right name in Klipper, it will invoke it
with the field's value.
"""

import asyncio
import logging
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Union
from pathlib import Path


import aiohttp
from jsonrpc_websocket import Server
import toml


PROGNAME = "spool2klipper"
CFG_DIR = "~/.config/" + PROGNAME
CFG_FILE = PROGNAME + ".cfg"


# pylint: disable=R0903
class Spool2Klipper:
    """Moonraker agent to send Spoolman's spool info to Klipper"""

    def __init__(self, config: Dict[str, Any]):
        self.gcode_macros: List[str] = []
        self.http_session = None
        self.moonraker_server = None
        self.moonraker_url = config[PROGNAME]["moonraker_url"]
        self.spoolman_url = config[PROGNAME]["spoolman_url"]
        self.klipper_spool_set_macro_prefix = config[PROGNAME][
            "klipper_spool_set_macro_prefix"
        ]
        self.klipper_spool_clear_macro = config[PROGNAME]["klipper_spool_clear_macro"]
        self.klipper_spool_done = config[PROGNAME]["klipper_spool_done"]
        self.spoolman_url = config[PROGNAME]["spoolman_url"]

    async def _fetch_spool_info(
        self, spool_id: Union[int, None]
    ) -> Optional[Union[Dict[str, Any], Exception]]:
        try:
            async with await self.http_session.get(
                f"{self.spoolman_url}/v1/spool/{spool_id}",
            ) as response:
                if response.status == 404:
                    return None
                if response.status == 200:
                    return await response.json()
                return Exception(await response.text())
        except aiohttp.client_exceptions.ClientConnectorError as e:
            return e

    async def _get_response_error(self, response: Exception) -> str:
        if isinstance(response, aiohttp.client_exceptions.ClientConnectorError):
            err_msg = f"Failed to connect to server: {response}"
        elif isinstance(response, Exception):
            err_msg = f"Unknown error: {response}"
        else:
            err_msg = f"Unknown error: {response}"
        return err_msg

    def _has_spoolman_set_macros(self) -> bool:
        prefix = self.klipper_spool_set_macro_prefix
        for k in self.gcode_macros:
            if k.startswith(prefix):
                return True
        return False

    async def _notify_active_spool_set(self, params: Dict[str, Any]) -> None:
        spool_id = params["spool_id"]
        if spool_id is not None:
            if self._has_spoolman_set_macros():
                logging.debug("Fetching data from Spoolman id=%s", spool_id)
                spool_data = await self._fetch_spool_info(spool_id)
                if spool_data is None:
                    logging.info("Spool ID %s not found, clearing fields", spool_id)
                    await self._run_gcode(self.klipper_spool_clear_macro)
                if isinstance(spool_data, Exception):
                    err_msg = self._get_response_error(spool_data)
                    logging.info("Attempt to fetch spool info failed: %s", err_msg)
                else:
                    spool_data: Dict[str, Any] = spool_data
                    logging.info("Fetched Spool data for ID %s", spool_id)
                    logging.debug("Got data from Spoolman: %s", spool_data)
                    await self._call_klipper_with_data(
                        self.klipper_spool_set_macro_prefix,
                        spool_data,
                    )
                    
                    if self.klipper_spool_done in self.gcode_macros:
                        await self._run_gcode(self.klipper_spool_done)
            else:
                logging.debug("No spoolman gcode set macros found")
        else:
            if self.klipper_spool_clear_macro in self.gcode_macros:
                await self._run_gcode(self.klipper_spool_clear_macro)
            else:
                logging.debug("No spoolman gcode clear macro found")

    async def _call_klipper_with_data(
        self,
        prefix: str,
        spool_data: Any,
    ) -> None:

        for key, val in spool_data.items():
            macro_name = prefix + key
            if isinstance(val, dict):
                await self._call_klipper_with_data(macro_name + "_", val)
            elif macro_name in self.gcode_macros:
                if isinstance(val, (int, float)):
                    script = f"{macro_name} VALUE={val}"
                else:
                    val = val.replace('"', "''")
                    script = f'{macro_name} VALUE="{val}"'
                await self._run_gcode(script)

    async def _run_gcode(self, script):
        logging.info("Run in klipper: '%s'", script)
        await self.moonraker_server.printer.gcode.script(
            script=script, _notification=True
        )

    async def _routine(self):
        async with aiohttp.ClientSession() as self.http_session:
            self.moonraker_server = Server(self.moonraker_url)
            try:
                await self.moonraker_server.ws_connect()

                objects = await self.moonraker_server.printer.objects.list()
                self.gcode_macros = [
                    x[12:] for x in objects["objects"] if x.startswith("gcode_macro ")
                ]
                logging.debug("Available macros: %s", (self.gcode_macros))

                self.moonraker_server.notify_active_spool_set = (
                    self._notify_active_spool_set
                )

                while True:
                    await asyncio.sleep(3600)
            finally:
                await self.moonraker_server.close()

    def run(self):
        """Run the agent in the async loop"""
        asyncio.get_event_loop().run_until_complete(self._routine())


if __name__ == "__main__":
    logging.basicConfig(encoding="utf-8", level=logging.DEBUG)
    config_data = None  # pylint: disable=C0103
    for path in ["~/" + CFG_FILE, CFG_DIR + "/" + CFG_FILE]:
        cfg_filename = os.path.expanduser(path)
        if os.path.exists(cfg_filename):
            with open(cfg_filename, "r", encoding="utf-8") as fp:
                config_data = toml.load(fp)
                break

    if not config_data:
        print(
            "WARNING: The config_data file is missing, installing a default version.",
            file=sys.stderr,
        )
        if not os.path.exists(CFG_DIR):
            cfg_dir = os.path.expanduser(CFG_DIR)
            print(f"Creating dir {cfg_dir}", file=sys.stderr)
            Path(cfg_dir).mkdir(parents=True, exist_ok=True)
        script_dir = os.path.dirname(__file__)
        from_filename = os.path.join(script_dir, CFG_FILE)
        to_filename = os.path.join(cfg_dir, CFG_FILE)
        shutil.copyfile(from_filename, to_filename)
        print(f"Created {to_filename}, please update it", file=sys.stderr)
        sys.exit(1)

    spool2klipper = Spool2Klipper(config_data)
    spool2klipper.run()
    