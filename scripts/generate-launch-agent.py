#!/usr/bin/env python3
"""Render the LaunchAgent plist using paths from the local installation."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from typing import Any


def replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    elif isinstance(value, list):
        return [replace(item, replacements) for item in value]
    elif isinstance(value, dict):
        return {key: replace(item, replacements) for key, item in value.items()}
    return value


def main() -> None:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: generate-launch-agent.py TEMPLATE OUTPUT PYTHON BRIDGE CONFIG APP_DIR LOG"
        )
    template, output, python, bridge, config, app_dir, log = sys.argv[1:]
    with Path(template).open("rb") as file:
        plist = plistlib.load(file)
    replacements = {
        "@@PYTHON@@": python,
        "@@BRIDGE@@": bridge,
        "@@CONFIG@@": config,
        "@@APP_DIR@@": app_dir,
        "@@LOG@@": log,
        "@@PATH@@": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    Path(output).write_bytes(plistlib.dumps(replace(plist, replacements)))


if __name__ == "__main__":
    main()
