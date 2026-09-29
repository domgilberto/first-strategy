"""
Non-secret tunables.

config.json in this repository is the configuration: what is committed is what
runs. Earlier versions seeded a copy onto the persistent volume once and let
that copy win, which froze every setting at its first-deploy value - nobody
can edit the volume, so a change in the repo never reached the bot (the live
bot ran a 60 s poll left over from the SMA version while the repo said 20).

A copy left on the volume by one of those versions is ignored. If it differs
from the committed file, the differences are logged and the copy is renamed to
config.json.retired, so the change is visible once and nothing is deleted.
"""

import json
import os

from runtime import log

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
ABSENT = "<absent>"


def differences(committed, other, prefix=""):
    """{dotted.key: {committed, volume}} for every value that differs."""
    out = {}
    for key in sorted(set(committed) | set(other)):
        a, b = committed.get(key, ABSENT), other.get(key, ABSENT)
        if isinstance(a, dict) and isinstance(b, dict):
            out.update(differences(a, b, prefix + key + "."))
        elif a != b:
            out[prefix + key] = {"committed": a, "volume": b}
    return out


def load_config(data_dir, path=CONFIG_PATH):
    with open(path) as fh:
        config = json.load(fh)

    old = os.path.join(data_dir, "config.json")
    if os.path.abspath(old) == os.path.abspath(path) or not os.path.exists(old):
        return config

    try:
        with open(old) as fh:
            diff = differences(config, json.load(fh))
    except (OSError, ValueError) as exc:
        diff = {"<unreadable>": str(exc)}
    if diff:
        log("warn", "Ignoring the config.json left on the volume - the committed config.json is what runs",
            path=old, differences=diff)
    try:
        os.replace(old, old + ".retired")
    except OSError as exc:
        log("warn", "Could not retire the old config.json", path=old, error=str(exc))
    return config
