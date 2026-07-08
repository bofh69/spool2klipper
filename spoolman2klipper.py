#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025 Sebastian Andersson <sebastian@bittr.nu>
# SPDX-FileCopyrightText: 2026 BlueBell-XA
# SPDX-License-Identifier: GPL-3.0-or-later

"""Synchronise active Spoolman filament data into Klipper macro variables."""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional, Set, Union
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from jsonrpc_websocket import Server
import toml


PROGNAME = "spoolman2klipper"
CFG_DIR = "~/.config/" + PROGNAME
CFG_FILE = PROGNAME + ".cfg"
DEFAULT_KLIPPER_MACRO = "SPOOLMAN"
LOG_FILE = PROGNAME + ".log"
LOG_MARKER_FILES = ("klippy.log", "moonraker.log", "crowsnest.log")
LOG_DIR_CANDIDATES = (
    "~/printer_data/logs",
    "~/klipper_logs",
    "~/logs",
)

SPOOL_VARS_DEFAULT: Dict[str, Any] = {
    "spool_id": -1,
    "filament_name": "",
    "material": "",
    "vendor": "",
    "color": "",
    "extruder_temp": 0,
    "bed_temp": 0,
    "diameter": 0.0,
    "density": 0.0,
    "remaining_weight": 0.0,
    "filament_weight": 0.0,
}


class Spoolman2Klipper:  # pylint: disable=too-many-instance-attributes
    """Moonraker agent that exposes active Spoolman data to Klipper macros."""

    def __init__(self, config: Dict[str, Any]):
        service_config = config[PROGNAME]
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.moonraker_server: Optional[Server] = None
        self.moonraker_url = service_config["moonraker_url"].rstrip("/")
        self.spoolman_url = service_config["spoolman_url"].rstrip("/")
        self.spoolman_ws_url = self.build_spoolman_websocket_url(self.spoolman_url)
        self.klipper_macro = service_config.get(
            "klipper_macro", DEFAULT_KLIPPER_MACRO
        )
        self.sync_on_connect = service_config.get("sync_on_connect", True)
        self.reconnect_delay = float(service_config.get("reconnect_delay", 2.0))
        self.request_timeout = float(service_config.get("request_timeout", 5.0))
        self.server_factory = Server
        self.is_closing = False
        self.macro_variables: Optional[Set[str]] = None
        self.active_spool_id: Optional[str] = None
        self.active_spool_data: Optional[Dict[str, Any]] = None

    def extract_spool_variables(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten the Spoolman spool payload into Klipper macro variables."""

        filament: Dict[str, Any] = data.get("filament") or {}
        vendor = filament.get("vendor") or {}
        if not isinstance(vendor, dict):
            vendor = {}

        return {
            "spool_id": data.get("id", SPOOL_VARS_DEFAULT["spool_id"]),
            "filament_name": filament.get("name")
            or SPOOL_VARS_DEFAULT["filament_name"],
            "material": filament.get("material") or SPOOL_VARS_DEFAULT["material"],
            "vendor": vendor.get("name") or SPOOL_VARS_DEFAULT["vendor"],
            "color": filament.get("color_hex") or SPOOL_VARS_DEFAULT["color"],
            "extruder_temp": filament.get("settings_extruder_temp")
            or SPOOL_VARS_DEFAULT["extruder_temp"],
            "bed_temp": filament.get("settings_bed_temp")
            or SPOOL_VARS_DEFAULT["bed_temp"],
            "diameter": filament.get("diameter") or SPOOL_VARS_DEFAULT["diameter"],
            "density": filament.get("density") or SPOOL_VARS_DEFAULT["density"],
            "remaining_weight": data.get("remaining_weight")
            or SPOOL_VARS_DEFAULT["remaining_weight"],
            "filament_weight": filament.get("weight")
            or SPOOL_VARS_DEFAULT["filament_weight"],
        }

    def format_set_variable_gcode(self, variable_name: str, value: Any) -> str:
        """Build a SET_GCODE_VARIABLE command for one Klipper macro variable."""

        if isinstance(value, str):
            safe_value = self._sanitise_gcode_string(value)
            formatted_value = f"\"'{safe_value}'\""
        else:
            formatted_value = str(value)

        return (
            f"SET_GCODE_VARIABLE MACRO={self.klipper_macro} "
            f"VARIABLE={variable_name} VALUE={formatted_value}"
        )

    async def push_klipper_variables(self, variables: Dict[str, Any]) -> None:
        """Push supported variables into Klipper, skipping undefined variables."""

        if self.moonraker_server is None:
            logging.debug("Skipping Klipper variable push; Moonraker is not connected")
            return
        if self.macro_variables is None:
            await self.detect_macro_variables()
            if self.macro_variables is None:
                logging.info(
                    "Skipping Klipper variable push; macro %s is not available",
                    self.klipper_macro,
                )
                return

        sent_count = 0
        skipped_variables = []
        failed_variables = []
        for variable_name, value in variables.items():
            if variable_name not in self.macro_variables:
                skipped_variables.append(variable_name)
                logging.debug(
                    "Skipping variable %s; macro %s does not define it",
                    variable_name,
                    self.klipper_macro,
                )
                continue

            script = self.format_set_variable_gcode(variable_name, value)
            try:
                await self._run_gcode(script)
                sent_count += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                failed_variables.append(variable_name)
                logging.warning(
                    "Failed to update Klipper variable %s: %s",
                    variable_name,
                    exc,
                )

        if sent_count == 0:
            logging.warning(
                "No Klipper variables were updated for macro %s; expected one of %s, "
                "detected %s, skipped %s, failed %s",
                self.klipper_macro,
                sorted(variables.keys()),
                sorted(self.macro_variables),
                sorted(skipped_variables),
                sorted(failed_variables),
            )
        else:
            logging.info(
                "Updated %s Klipper variable(s) for macro %s; skipped %s, failed %s",
                sent_count,
                self.klipper_macro,
                len(skipped_variables),
                len(failed_variables),
            )

    async def notify_active_spool_set(self, params: Dict[str, Any]) -> None:
        """Handle Moonraker's active spool notification payload."""

        spool_id = params.get("spool_id")
        logging.info("Moonraker active spool changed to %s", spool_id)
        self.active_spool_id = normalize_spool_id(spool_id)
        if spool_id is None:
            self.active_spool_data = None
            logging.info("Clearing Klipper spool variables")
            await self.push_klipper_variables(SPOOL_VARS_DEFAULT)
            return

        logging.info("Fetching Spoolman data for active spool %s", spool_id)
        spool_data = await self.fetch_spool_info(spool_id)
        if spool_data is None:
            logging.info("Spool ID %s not found, clearing Klipper variables", spool_id)
            self.active_spool_data = None
            await self.push_klipper_variables(SPOOL_VARS_DEFAULT)
            return

        if isinstance(spool_data, Exception):
            logging.info(
                "Attempt to fetch Spoolman data for spool %s failed: %s",
                spool_id,
                spool_data,
            )
            return

        self.active_spool_data = spool_data
        logging.info("Updating Klipper spool variables for active spool %s", spool_id)
        await self.push_klipper_variables(self.extract_spool_variables(spool_data))

    async def fetch_spool_info(
        self, spool_id: Union[int, str]
    ) -> Optional[Union[Dict[str, Any], Exception]]:
        """Fetch one spool from the configured Spoolman HTTP API."""

        if self.http_session is None:
            raise RuntimeError("HTTP session is not initialised")

        try:
            async with self.http_session.get(
                f"{self.spoolman_url}/v1/spool/{spool_id}",
                timeout=self.request_timeout,
            ) as response:
                if response.status == 404:
                    return None
                if response.status == 200:
                    return await response.json()
                return Exception(await response.text())
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return exc

    async def handle_spoolman_event(self, event: Dict[str, Any]) -> None:
        """Handle one event from Spoolman's spool websocket."""

        if event.get("resource") != "spool":
            return

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return

        spool_id = payload.get("id")
        if normalize_spool_id(spool_id) != normalize_spool_id(self.active_spool_id):
            return

        event_type = event.get("type")
        if event_type == "updated":
            logging.info("Spoolman updated active spool %s", spool_id)
            self.active_spool_data = payload
            await self.push_klipper_variables(self.extract_spool_variables(payload))
        elif event_type == "deleted":
            logging.info("Spoolman deleted active spool %s; clearing Klipper state", spool_id)
            self.active_spool_id = None
            self.active_spool_data = None
            await self.push_klipper_variables(SPOOL_VARS_DEFAULT)

    async def handle_spoolman_status_changed(self, params: Dict[str, Any]) -> None:
        """Log Moonraker's view of its Spoolman connection state."""

        logging.info(
            "Moonraker reports Spoolman connection status: %s",
            params.get("spoolman_connected"),
        )

    async def handle_klippy_ready(self, *_args: Any, **_kwargs: Any) -> None:
        """Refresh macro state after Klipper becomes ready."""

        logging.info("Klipper is ready; refreshing %s macro state", self.klipper_macro)
        await self.detect_macro_variables()
        if self.active_spool_data is not None:
            await self.push_klipper_variables(
                self.extract_spool_variables(self.active_spool_data)
            )
            return
        if self.sync_on_connect:
            await self.sync_current_active_spool()

    async def handle_klippy_disconnected(self, *_args: Any, **_kwargs: Any) -> None:
        """Clear cached macro metadata after Klipper disconnects or shuts down."""

        logging.info("Klipper disconnected or shut down; clearing macro metadata cache")
        self.macro_variables = None

    async def detect_macro_variables(self) -> None:
        """Detect the configured Klipper macro and its available variables."""

        if self.moonraker_server is None:
            self.macro_variables = None
            return

        try:
            objects = await self.moonraker_server.printer.objects.list()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Unable to list Klipper objects: %s", exc)
            self.macro_variables = None
            return

        macro_key = f"gcode_macro {self.klipper_macro}"
        if macro_key not in objects.get("objects", []):
            logging.info("Klipper macro %s was not found", self.klipper_macro)
            self.macro_variables = None
            return

        try:
            result = await self.moonraker_server.printer.objects.query(
                objects={macro_key: None}
            )
            status = result.get("status", result)
            macro_state = status.get(macro_key, {})
            self.macro_variables = {
                normalize_macro_variable_name(variable_name)
                for variable_name in macro_state.keys()
            }
            logging.info(
                "Detected Klipper macro %s with %s variable(s): %s",
                self.klipper_macro,
                len(self.macro_variables),
                ", ".join(sorted(self.macro_variables)),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning(
                "Detected macro %s but could not query variables: %s",
                self.klipper_macro,
                exc,
            )
            self.macro_variables = None

    async def sync_current_active_spool(self) -> None:
        """Optionally pull Moonraker's current active spool on service startup."""

        if self.moonraker_server is None:
            return

        try:
            response = await self.moonraker_server.server.spoolman.get_spool_id()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Unable to query current Moonraker active spool: %s", exc)
            return

        spool_id = self.parse_spool_id_response(response)
        logging.info("Moonraker current active spool is %s", spool_id)
        await self.notify_active_spool_set({"spool_id": spool_id})

    async def _run_gcode(self, script: str) -> None:
        """Run one G-code script through Moonraker."""

        logging.info("Run in Klipper: %s", script)
        await self.moonraker_server.printer.gcode.script(  # type: ignore[union-attr]
            script=script,
            _notification=False,
        )

    async def moonraker_connection_loop(self, max_cycles: Optional[int] = None) -> None:
        """Reconnect to Moonraker whenever the websocket receive loop exits."""

        cycles = 0
        while not self.is_closing:
            if max_cycles is not None and cycles >= max_cycles:
                return
            cycles += 1
            try:
                await self.moonraker_connection_cycle()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Moonraker connection cycle failed: %s", exc)
            finally:
                self.moonraker_server = None
                self.macro_variables = None

            if not self.is_closing and (
                max_cycles is None or cycles < max_cycles
            ):
                await asyncio.sleep(self.reconnect_delay)

    async def moonraker_connection_cycle(self) -> None:
        """Run one Moonraker websocket connection until it closes."""

        logging.info("Connecting to Moonraker websocket: %s", self.moonraker_url)
        self.moonraker_server = self.server_factory(self.moonraker_url)
        receive_task = None
        try:
            receive_task = await self.moonraker_server.ws_connect()
            logging.info("Connected to Moonraker websocket")
            self.register_moonraker_notification_handlers()
            await self.detect_macro_variables()

            if self.sync_on_connect:
                await self.sync_current_active_spool()

            await receive_task
        finally:
            logging.info("Moonraker websocket disconnected")
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
            await self.moonraker_server.close()

    def register_moonraker_notification_handlers(self) -> None:
        """Register notification callbacks used by Moonraker and Klipper events."""

        if self.moonraker_server is None:
            return
        self.moonraker_server.notify_active_spool_set = self.notify_active_spool_set
        self.moonraker_server.notify_klippy_ready = self.handle_klippy_ready
        self.moonraker_server.notify_klippy_shutdown = self.handle_klippy_disconnected
        self.moonraker_server.notify_klippy_disconnected = (
            self.handle_klippy_disconnected
        )
        self.moonraker_server.notify_spoolman_status_changed = (
            self.handle_spoolman_status_changed
        )

    async def spoolman_connection_loop(self) -> None:
        """Reconnect to Spoolman's websocket whenever it exits."""

        while not self.is_closing:
            try:
                await self.spoolman_connection_cycle()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Spoolman websocket cycle failed: %s", exc)

            if not self.is_closing:
                await asyncio.sleep(self.reconnect_delay)

    async def spoolman_connection_cycle(self) -> None:
        """Read one Spoolman websocket connection until it closes."""

        if self.http_session is None:
            raise RuntimeError("HTTP session is not initialised")

        logging.info("Connecting to Spoolman websocket: %s", self.spoolman_ws_url)
        async with self.http_session.ws_connect(
            self.spoolman_ws_url,
            heartbeat=20,
            timeout=self.request_timeout,
        ) as websocket:
            logging.info("Connected to Spoolman websocket")
            async for message in websocket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self.handle_spoolman_event(json.loads(message.data))
                    except (TypeError, json.JSONDecodeError) as exc:
                        logging.debug("Ignored invalid Spoolman websocket event: %s", exc)
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise aiohttp.ClientError("Spoolman websocket error")
        logging.info("Spoolman websocket disconnected")

    async def _routine(self) -> None:
        async with aiohttp.ClientSession() as self.http_session:
            moonraker_task = asyncio.create_task(self.moonraker_connection_loop())
            spoolman_task = asyncio.create_task(self.spoolman_connection_loop())
            try:
                await asyncio.gather(moonraker_task, spoolman_task)
            finally:
                self.is_closing = True
                for task in (moonraker_task, spoolman_task):
                    if not task.done():
                        task.cancel()

    def run(self) -> None:
        """Run the long-lived service in the default async event loop."""

        asyncio.get_event_loop().run_until_complete(self._routine())

    @staticmethod
    def parse_spool_id_response(response: Dict[str, Any]) -> Optional[Union[int, str]]:
        """Extract Moonraker's active spool ID from known response shapes."""

        if "spool_id" in response:
            return response.get("spool_id")

        result = response.get("result")
        if isinstance(result, dict):
            return result.get("spool_id")

        return None

    @staticmethod
    def build_spoolman_websocket_url(spoolman_url: str) -> str:
        """Build Spoolman's spool websocket URL from the configured API URL."""

        parts = urlsplit(spoolman_url.rstrip("/"))
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = parts.path.rstrip("/") + "/v1/spool"
        return urlunsplit((scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _sanitise_gcode_string(value: str) -> str:
        """Keep macro string values on one line and remove quote delimiters."""

        return (
            value.replace("'", "")
            .replace('"', "")
            .replace("\n", " ")
            .replace("\r", " ")
        )


def normalize_macro_variable_name(variable_name: str) -> str:
    """Convert Klipper storage keys into SET_GCODE_VARIABLE names."""

    if variable_name.startswith("variable_"):
        return variable_name[len("variable_") :]
    return variable_name


def normalize_spool_id(spool_id: Optional[Union[int, str]]) -> Optional[str]:
    """Normalize spool IDs from HTTP/websocket payloads for comparisons."""

    if spool_id is None:
        return None
    return str(spool_id)


def resolve_log_file(configured_log_file: Optional[str] = None) -> str:
    """Resolve the service log file path into Klipper's visible log directory."""

    if configured_log_file:
        return os.path.expanduser(configured_log_file)

    for log_dir in LOG_DIR_CANDIDATES:
        expanded_dir = os.path.expanduser(log_dir)
        if any(
            os.path.exists(os.path.join(expanded_dir, marker_file))
            for marker_file in LOG_MARKER_FILES
        ):
            return os.path.join(expanded_dir, LOG_FILE)

    return os.path.join(os.path.expanduser(LOG_DIR_CANDIDATES[0]), LOG_FILE)


def configure_logging(log_file: str, stderr: Any = sys.stderr) -> None:
    """Send timestamped logs to a printer-web-UI-visible file and stderr."""

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(stderr)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


def load_config() -> Optional[Dict[str, Any]]:
    """Load user configuration from the supported config locations."""

    script_dir = os.path.dirname(os.path.abspath(__file__))
    search_paths = [
        "~/printer_data/config/" + CFG_FILE,
        "~/klipper_config/" + CFG_FILE,
        "~/" + CFG_FILE,
        CFG_DIR + "/" + CFG_FILE,
        os.path.join(script_dir, CFG_FILE),
    ]

    for path in search_paths:
        cfg_filename = os.path.expanduser(path)
        if os.path.exists(cfg_filename):
            try:
                with open(cfg_filename, "r", encoding="utf-8") as file_pointer:
                    return toml.load(file_pointer)
            except (OSError, toml.TomlDecodeError) as exc:
                logging.error("Failed to load config from %s: %s", cfg_filename, exc)
    return None


if __name__ == "__main__":
    config_data = load_config()
    if not config_data:
        configure_logging(resolve_log_file())
        print("ERROR: Missing configuration file in all supported locations.", file=sys.stderr)
        sys.exit(1)

    service_settings = config_data.get(PROGNAME, {})
    log_filename = resolve_log_file(service_settings.get("log_file"))
    configure_logging(log_filename)
    spoolman2klipper = Spoolman2Klipper(config_data)
    logging.info(
        "Starting %s with Moonraker %s and Spoolman API %s; logging to %s",
        PROGNAME,
        spoolman2klipper.moonraker_url,
        spoolman2klipper.spoolman_url,
        log_filename,
    )
    spoolman2klipper.run()
