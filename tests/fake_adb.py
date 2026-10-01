#!/usr/bin/env python3
"""A fake ``adb``, for the transport tests.

It emulates the three commands the adb transport issues — ``devices``, ``shell``
and ``pull`` — against a local directory standing in for the device. The device
shell commands reference ``/sdcard``, which is rewritten to that directory, so the
real command strings and the real parsers are exercised end to end.

The local userland (GNU) is close enough to the device one (toybox) for the flags
used here: ``stat -c``, ``head -c``, ``tail -c``, ``sha256sum``, ``xargs -0`` and
``df -k`` all behave the same way for what this program reads.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

FAKE_SERIAL = "FAKESERIAL"
DEVICE_ROOT = "/sdcard"


def fake_root() -> str:
    """Return the local directory standing in for the device storage."""
    root = os.environ.get("FAKE_ADB_ROOT")
    if not root:
        print("FAKE_ADB_ROOT is not set", file=sys.stderr)
        raise SystemExit(2)
    return root


def rewrite(command: str) -> str:
    """Map the device paths of a shell command to the fake device."""
    return command.replace(DEVICE_ROOT, fake_root())


def main(argv: list[str]) -> int:
    arguments = list(argv)
    if arguments and arguments[0] == "-s":
        del arguments[:2]  # the serial is ignored: there is only one fake device
    if not arguments:
        print("fake adb: nothing to do", file=sys.stderr)
        return 2
    command = arguments[0]
    if command == "devices":
        print("List of devices attached")
        print(f"{FAKE_SERIAL}\tdevice")
        return 0
    if command == "shell":
        remote = rewrite(" ".join(arguments[1:]))
        # The device paths also travel on stdin (batches of paths for xargs), so
        # the payload is rewritten the same way the command is.
        payload = rewrite(sys.stdin.buffer.read().decode("utf-8", "replace"))
        completed = subprocess.run(
            ["sh", "-c", remote],
            input=payload.encode("utf-8", "replace"),
            capture_output=True,
        )
        # The device sees `/sdcard`: the answer is rewritten back, since only
        # this process knows where the fake storage really is.
        for stream, content in (
            (sys.stdout, completed.stdout),
            (sys.stderr, completed.stderr),
        ):
            stream.write(content.decode("utf-8", "replace").replace(fake_root(), DEVICE_ROOT))
        return completed.returncode
    if command == "pull":
        source, destination = arguments[1], arguments[2]
        local_source = rewrite(source)
        if not Path(local_source).is_file():
            print(f"adb: error: failed to stat remote object '{source}'", file=sys.stderr)
            return 1
        shutil.copyfile(local_source, destination)
        return 0
    print(f"fake adb: unsupported command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
