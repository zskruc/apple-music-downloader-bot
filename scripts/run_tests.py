#!/usr/bin/env python3
"""Run the shipped regression suites without network or production state."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
REAL_CONNECT = socket.socket.connect
REAL_CONNECT_EX = socket.socket.connect_ex
REAL_CREATE_CONNECTION = socket.create_connection


def fixture_address(address) -> bool:
    return isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}


def fixture_connect(client, address):
    if not fixture_address(address):
        raise RuntimeError("External connections are disabled during repository tests")
    return REAL_CONNECT(client, address)


def fixture_connect_ex(client, address):
    if not fixture_address(address):
        raise RuntimeError("External connections are disabled during repository tests")
    return REAL_CONNECT_EX(client, address)


def fixture_connection(address, *args, **kwargs):
    if not fixture_address(address):
        raise RuntimeError("External connections are disabled during repository tests")
    return REAL_CREATE_CONNECTION(address, *args, **kwargs)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="amdl-tests-") as directory:
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith(("AMDL_", "WORD_LYRICS_", "TELEGRAM_", "SYNOLOGY_", "YT_", "BILI_", "LX_", "SQ_"))}
        clean.update({
            "AMDL_STATE_DIR": directory + "/state",
            "AMDL_LOG_DIR": directory + "/logs",
            "AMDL_TEMP_PATH": directory + "/tmp",
            "YT_STATE_DIR": directory + "/youtube",
            "BILI_STATE_DIR": directory + "/bilibili",
            "AMDL_WORD_LYRICS_STATE_DIR": directory + "/lyrics",
            "TELEGRAM_BOT_TOKEN": "",
            "SYNOLOGY_CHAT_ENABLED": "false",
        })
        with patch.dict(os.environ, clean, clear=True), \
                patch.object(socket.socket, "connect", fixture_connect), \
                patch.object(socket.socket, "connect_ex", fixture_connect_ex), \
                patch.object(socket, "create_connection", fixture_connection):
            suites = unittest.TestSuite()
            loader = unittest.TestLoader()
            for group in (ROOT / "tests").iterdir():
                if group.is_dir() and group.name != "__pycache__":
                    suites.addTests(loader.discover(str(group), pattern="test_*.py"))
                    loader = unittest.TestLoader()
            suites.addTests(loader.discover(str(ROOT / "ops"), pattern="test_*.py"))
            for name in ("lx-music", "sq-music"):
                loader = unittest.TestLoader()
                suites.addTests(loader.discover(str(ROOT / "services" / name), pattern="test_*.py"))
            result = unittest.TextTestRunner(verbosity=1).run(suites)
    return 0 if result.wasSuccessful() and result.testsRun else 1


if __name__ == "__main__":
    raise SystemExit(main())
