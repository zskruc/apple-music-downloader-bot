"""Run yt-dlp with private Cookie snapshots instead of its shared login file.

yt-dlp normally saves its Cookie jar when exiting. A download that started
before a browser refresh must never write its older session over that refresh.
Each invocation gets a mode-0600 copy, including independent subtitle/probe
processes, while HTTP Cookie updates remain usable inside that invocation.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any


SABR_TOOL_SHA256 = "8e851af222deaeeced7a9444a3c961fa054dc850e92d400e3ff73110f7bd9f4d"


def _private_cookie_arguments(arguments: list[str], directory: Path) -> list[str]:
    result = list(arguments)
    index = 0
    cookie_number = 0
    while index < len(result):
        argument = result[index]
        if argument == "--cookies":
            if index + 1 >= len(result):
                # Let yt-dlp report the original CLI usage error.
                break
            value_index = index + 1
            source = result[value_index]
        elif argument.startswith("--cookies="):
            value_index = index
            source = argument.partition("=")[2]
        else:
            index += 1
            continue
        cookie_number += 1
        destination = directory / f"cookies-{cookie_number}.txt"
        # Exclusive creation and fixed permissions also protect copies when the
        # caller uses a permissive umask. Never copy the source's permissions.
        with destination.open("xb") as output:
            destination.chmod(0o600)
            with Path(source).expanduser().open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
        result[value_index] = (
            f"--cookies={destination}" if value_index == index else str(destination)
        )
        index = value_index + 1
    return result


def main(argv: list[str] | None = None) -> Any:
    arguments = list(sys.argv[1:] if argv is None else argv)
    tool = None
    if arguments[:1] == ["--sabr-tool"]:
        if len(arguments) < 2:
            raise SystemExit("缺少固定 SABR 工具路径")
        tool = Path(arguments[1])
        try:
            valid = hashlib.sha256(tool.read_bytes()).hexdigest() == SABR_TOOL_SHA256
        except OSError:
            valid = False
        if not valid:
            raise SystemExit("专用 YouTube SABR 工具完整性校验失败")
        arguments = arguments[2:]
    with tempfile.TemporaryDirectory(prefix="amdl-ytdlp-cookies-") as temporary:
        directory = Path(temporary)
        previous_handler = None

        def terminate(signum: int, _frame: Any) -> None:
            shutil.rmtree(directory, ignore_errors=True)
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.signal(signal.SIGTERM, terminate)
        if tool is not None:
            sys.path.insert(0, str(tool))
        try:
            private_arguments = _private_cookie_arguments(arguments, directory)
            import yt_dlp

            return yt_dlp.main(private_arguments)
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
            if tool is not None:
                sys.path.remove(str(tool))


if __name__ == "__main__":
    raise SystemExit(main())
