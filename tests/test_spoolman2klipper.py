"""Tests for the spoolman2klipper service contract."""

# SPDX-FileCopyrightText: 2026 BlueBell-XA
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=missing-function-docstring,protected-access,too-few-public-methods

import asyncio
import io
import logging
import re
import sys

import aiohttp
import pytest

import spoolman2klipper
from spoolman2klipper import SPOOL_VARS_DEFAULT, Spoolman2Klipper, load_config


class FakeSpoolmanResponse:
    """Small async context manager that mimics aiohttp's response shape."""

    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeHttpSession:
    """Captures requested URLs and returns predefined Spoolman responses."""

    def __init__(self, responses):
        self.responses = responses
        self.requested_urls = []

    def get(self, url, **_kwargs):
        self.requested_urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeObjectsApi:
    """Provides the Moonraker printer.objects API used by macro detection."""

    def __init__(self, objects=None, query_result=None):
        self.objects = objects or ["gcode_macro SPOOLMAN"]
        self.query_result = query_result or {
            "status": {"gcode_macro SPOOLMAN": {"spool_id": -1, "material": ""}}
        }
        self.list_calls = 0
        self.query_calls = []

    async def list(self):
        self.list_calls += 1
        return {"objects": self.objects}

    async def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return self.query_result


class FakeGcodeApi:
    """Records G-code commands sent through the fake Moonraker client."""

    def __init__(self):
        self.scripts = []

    async def script(self, script, _notification=True):
        self.scripts.append((script, _notification))


class FakePrinter:
    """Provides the nested printer.gcode.script API used by Moonraker."""

    def __init__(self):
        self.gcode = FakeGcodeApi()
        self.objects = FakeObjectsApi()


class FakeSpoolmanApi:
    """Provides the Moonraker server.spoolman API shape used by startup sync."""

    def __init__(self, response):
        self.response = response
        self.get_spool_id_calls = 0

    async def get_spool_id(self):
        self.get_spool_id_calls += 1
        return self.response


class FakeServerNamespace:
    """Provides nested Moonraker server APIs used by startup sync."""

    def __init__(self, spool_id_response=None):
        self.spoolman = FakeSpoolmanApi(spool_id_response or {"spool_id": None})


class FakeMoonrakerServer:
    """Fake Moonraker server with only the API surface required by tests."""

    def __init__(self, spool_id_response=None):
        self.printer = FakePrinter()
        self.server = FakeServerNamespace(spool_id_response)


class DisconnectingMoonrakerServer(FakeMoonrakerServer):
    """Moonraker fake whose websocket receive loop ends immediately."""

    def __init__(self):
        super().__init__()
        self.closed = False
        self.notification_handlers = {}

    def __setattr__(self, name, value):
        if not name.startswith("_") and name.startswith("notify_"):
            self.notification_handlers[name] = value
        object.__setattr__(self, name, value)

    async def ws_connect(self):
        return asyncio.create_task(asyncio.sleep(0))

    async def close(self):
        self.closed = True


def make_service(macro_variables=None):
    """Build a service instance with fake dependencies for unit tests."""

    config = {
        "spoolman2klipper": {
            "moonraker_url": "ws://moonraker.local/websocket",
            "spoolman_url": "http://spoolman.local/api",
            "klipper_macro": "SPOOLMAN",
        }
    }
    service = Spoolman2Klipper(config)
    service.moonraker_server = FakeMoonrakerServer()
    service.macro_variables = set(macro_variables or SPOOL_VARS_DEFAULT)
    return service


def representative_spool_payload():
    """Return a Spoolman spool payload with the fields used by the service."""

    return {
        "id": 123,
        "remaining_weight": 742.5,
        "filament": {
            "name": "PLA+ Black",
            "material": "PLA",
            "color_hex": "1A1A1A",
            "settings_extruder_temp": 210,
            "settings_bed_temp": 60,
            "diameter": 1.75,
            "density": 1.24,
            "weight": 1000.0,
            "vendor": {"name": "Example Filaments"},
        },
    }


def test_extract_spool_variables_flattens_spoolman_payload():
    service = make_service()

    variables = service.extract_spool_variables(representative_spool_payload())

    assert variables == {
        "spool_id": 123,
        "filament_name": "PLA+ Black",
        "material": "PLA",
        "vendor": "Example Filaments",
        "color": "1A1A1A",
        "extruder_temp": 210,
        "bed_temp": 60,
        "diameter": 1.75,
        "density": 1.24,
        "remaining_weight": 742.5,
        "filament_weight": 1000.0,
    }


def test_extract_spool_variables_uses_defaults_for_missing_nested_data():
    service = make_service()

    variables = service.extract_spool_variables({"id": 456})

    assert variables == {
        **SPOOL_VARS_DEFAULT,
        "spool_id": 456,
    }


def test_resolve_log_file_prefers_directory_with_existing_klipper_log(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    logs_dir = fake_home / "klipper_logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "moonraker.log").write_text("existing log\n", encoding="utf-8")

    def fake_expanduser(path):
        if path.startswith("~/"):
            return str(fake_home / path[2:])
        return path

    monkeypatch.setattr("spoolman2klipper.os.path.expanduser", fake_expanduser)

    assert spoolman2klipper.resolve_log_file() == str(
        logs_dir / "spoolman2klipper.log"
    )


def test_configure_logging_writes_timestamped_log_file(tmp_path):
    log_file = tmp_path / "spoolman2klipper.log"
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    try:
        spoolman2klipper.configure_logging(str(log_file), stderr=sys.stderr)
        logging.info("file logging smoke test")
    finally:
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)

    log_text = log_file.read_text(encoding="utf-8")
    assert "INFO file logging smoke test" in log_text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", log_text)


def test_format_set_variable_gcode_quotes_string_values():
    service = make_service()

    command = service.format_set_variable_gcode("filament_name", 'PLA "Pro"')

    assert (
        command
        == 'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=filament_name VALUE="\'PLA Pro\'"'
    )


def test_format_set_variable_gcode_leaves_numeric_values_unquoted():
    service = make_service()

    command = service.format_set_variable_gcode("extruder_temp", 210)

    assert command == "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=extruder_temp VALUE=210"


def test_normalize_macro_variable_name_accepts_klipper_storage_prefix():
    assert (
        spoolman2klipper.normalize_macro_variable_name("variable_spool_id")
        == "spool_id"
    )
    assert spoolman2klipper.normalize_macro_variable_name("spool_id") == "spool_id"


@pytest.mark.asyncio
async def test_run_gcode_waits_for_moonraker_result_instead_of_sending_notification():
    service = make_service()

    await service._run_gcode("SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123")

    assert service.moonraker_server.printer.gcode.scripts == [
        ("SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123", False)
    ]


@pytest.mark.asyncio
async def test_push_klipper_variables_only_sends_defined_macro_variables():
    service = make_service(macro_variables={"spool_id", "material"})

    await service.push_klipper_variables(
        {
            "spool_id": 123,
            "material": "PETG",
            "vendor": "Skipped Vendor",
        }
    )

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=material VALUE="\'PETG\'"',
    ]


@pytest.mark.asyncio
async def test_push_klipper_variables_warns_when_no_variables_are_sent(caplog):
    service = make_service(macro_variables={"unrelated"})

    with caplog.at_level(logging.WARNING):
        await service.push_klipper_variables({"spool_id": 123, "material": "PETG"})

    assert "No Klipper variables were updated for macro SPOOLMAN" in caplog.text
    assert "spool_id" in caplog.text
    assert "material" in caplog.text
    assert "unrelated" in caplog.text


@pytest.mark.asyncio
async def test_notify_active_spool_set_fetches_spool_and_pushes_variables():
    service = make_service(macro_variables={"spool_id", "filament_name", "bed_temp"})
    service.http_session = FakeHttpSession(
        [FakeSpoolmanResponse(200, representative_spool_payload())]
    )

    await service.notify_active_spool_set({"spool_id": 123})

    assert service.http_session.requested_urls == ["http://spoolman.local/api/v1/spool/123"]
    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=filament_name VALUE="\'PLA+ Black\'"',
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=bed_temp VALUE=60",
    ]


@pytest.mark.asyncio
async def test_notify_active_spool_set_clears_defaults_when_spool_is_none():
    service = make_service(macro_variables={"spool_id", "filament_name", "bed_temp"})

    await service.notify_active_spool_set({"spool_id": None})

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=-1",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=filament_name VALUE="\'\'"',
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=bed_temp VALUE=0",
    ]


@pytest.mark.asyncio
async def test_notify_active_spool_set_clears_defaults_when_spool_is_missing():
    service = make_service(macro_variables={"spool_id"})
    service.http_session = FakeHttpSession([FakeSpoolmanResponse(404)])

    await service.notify_active_spool_set({"spool_id": 999})

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == ["SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=-1"]


def test_parse_spool_id_response_accepts_direct_and_wrapped_shapes():
    service = make_service()

    assert service.parse_spool_id_response({"spool_id": 321}) == 321
    assert service.parse_spool_id_response({"result": {"spool_id": 654}}) == 654
    assert service.parse_spool_id_response({"result": {"spool_id": None}}) is None


@pytest.mark.asyncio
async def test_startup_sync_uses_documented_moonraker_get_spool_id_method():
    service = make_service(macro_variables={"spool_id"})
    service.moonraker_server = FakeMoonrakerServer({"spool_id": 123})
    service.http_session = FakeHttpSession(
        [FakeSpoolmanResponse(200, representative_spool_payload())]
    )

    await service.sync_current_active_spool()

    assert service.moonraker_server.server.spoolman.get_spool_id_calls == 1
    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == ["SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123"]


@pytest.mark.asyncio
async def test_fetch_spool_info_converts_aiohttp_and_timeout_errors_to_exceptions():
    service = make_service()
    service.http_session = FakeHttpSession(
        [
            aiohttp.ClientError("connection dropped"),
            asyncio.TimeoutError(),
        ]
    )

    assert isinstance(await service.fetch_spool_info(123), Exception)
    assert isinstance(await service.fetch_spool_info(456), Exception)


@pytest.mark.asyncio
async def test_spoolman_updated_event_refreshes_active_spool_variables():
    service = make_service(macro_variables={"spool_id", "material"})
    service.active_spool_id = 123

    await service.handle_spoolman_event(
        {
            "resource": "spool",
            "type": "updated",
            "payload": {
                "id": 123,
                "filament": {"material": "ASA"},
            },
        }
    )

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=material VALUE="\'ASA\'"',
    ]


@pytest.mark.asyncio
async def test_spoolman_updated_event_matches_active_spool_id_as_string_or_int():
    service = make_service(macro_variables={"spool_id", "material"})
    service.active_spool_id = "123"

    await service.handle_spoolman_event(
        {
            "resource": "spool",
            "type": "updated",
            "payload": {
                "id": 123,
                "filament": {"material": "ASA"},
            },
        }
    )

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=material VALUE="\'ASA\'"',
    ]


@pytest.mark.asyncio
async def test_spoolman_updated_event_logs_active_spool_refresh(caplog):
    service = make_service(macro_variables={"spool_id"})
    service.active_spool_id = 123

    with caplog.at_level(logging.INFO):
        await service.handle_spoolman_event(
            {"resource": "spool", "type": "updated", "payload": {"id": 123}}
        )

    assert "Spoolman updated active spool 123" in caplog.text


@pytest.mark.asyncio
async def test_spoolman_deleted_event_clears_active_spool_variables():
    service = make_service(macro_variables={"spool_id"})
    service.active_spool_id = 123

    await service.handle_spoolman_event(
        {"resource": "spool", "type": "deleted", "payload": {"id": 123}}
    )

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == ["SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=-1"]
    assert service.active_spool_id is None


@pytest.mark.asyncio
async def test_klippy_ready_redetects_macro_and_repushes_cached_data():
    service = make_service(macro_variables=set())
    service.active_spool_id = 123
    service.active_spool_data = representative_spool_payload()
    service.moonraker_server.printer.objects = FakeObjectsApi(
        query_result={
            "status": {
                "gcode_macro SPOOLMAN": {
                    "spool_id": -1,
                    "filament_name": "",
                }
            }
        }
    )

    await service.handle_klippy_ready()

    scripts = [script for script, _notify in service.moonraker_server.printer.gcode.scripts]
    assert scripts == [
        "SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=spool_id VALUE=123",
        'SET_GCODE_VARIABLE MACRO=SPOOLMAN VARIABLE=filament_name VALUE="\'PLA+ Black\'"',
    ]


@pytest.mark.asyncio
async def test_detect_macro_variables_normalizes_klipper_storage_prefix():
    service = make_service(macro_variables=set())
    service.moonraker_server.printer.objects = FakeObjectsApi(
        query_result={
            "status": {
                "gcode_macro SPOOLMAN": {
                    "variable_spool_id": -1,
                    "variable_filament_name": "",
                }
            }
        }
    )

    await service.detect_macro_variables()

    assert service.macro_variables == {"spool_id", "filament_name"}


@pytest.mark.asyncio
async def test_moonraker_connection_loop_reconnects_after_disconnect():
    created_servers = []

    def server_factory(_url):
        server = DisconnectingMoonrakerServer()
        created_servers.append(server)
        return server

    service = make_service()
    service.server_factory = server_factory
    service.reconnect_delay = 0

    await service.moonraker_connection_loop(max_cycles=2)

    assert len(created_servers) == 2
    assert all(server.closed for server in created_servers)
    assert "notify_active_spool_set" in created_servers[-1].notification_handlers
    assert "notify_klippy_ready" in created_servers[-1].notification_handlers


def test_load_config_skips_invalid_toml_and_uses_next_existing_file(monkeypatch):
    first_path = "~/printer_data/config/spoolman2klipper.cfg"
    second_path = "~/klipper_config/spoolman2klipper.cfg"

    monkeypatch.setattr("spoolman2klipper.os.path.expanduser", lambda path: path)
    monkeypatch.setattr(
        "spoolman2klipper.os.path.exists",
        lambda path: path in {first_path, second_path},
    )

    def fake_open(path, *_args, **_kwargs):
        if path == first_path:
            return io.StringIO('invalid = "unterminated')
        if path == second_path:
            return io.StringIO(
                "[spoolman2klipper]\n"
                "moonraker_url = 'ws://moonraker'\n"
                "spoolman_url = 'http://spoolman'\n"
            )
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)

    assert load_config() == {
        "spoolman2klipper": {
            "moonraker_url": "ws://moonraker",
            "spoolman_url": "http://spoolman",
        }
    }


def test_load_config_skips_unreadable_file_and_uses_next_existing_file(monkeypatch):
    first_path = "~/printer_data/config/spoolman2klipper.cfg"
    second_path = "~/klipper_config/spoolman2klipper.cfg"

    monkeypatch.setattr("spoolman2klipper.os.path.expanduser", lambda path: path)
    monkeypatch.setattr(
        "spoolman2klipper.os.path.exists",
        lambda path: path in {first_path, second_path},
    )

    def fake_open(path, *_args, **_kwargs):
        if path == first_path:
            raise OSError("permission denied")
        if path == second_path:
            return io.StringIO(
                "[spoolman2klipper]\n"
                "moonraker_url = 'ws://moonraker'\n"
                "spoolman_url = 'http://spoolman'\n"
            )
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", fake_open)

    assert load_config() == {
        "spoolman2klipper": {
            "moonraker_url": "ws://moonraker",
            "spoolman_url": "http://spoolman",
        }
    }
