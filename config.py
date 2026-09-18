"""
Non-secret tunables.

config.json lives on the persistent volume and is only seeded when absent, so a
config written by an earlier version of this strategy survives a deploy and can
be missing keys the new code expects. Merge it over the committed defaults
rather than trusting it to be complete, and say what was missing.
"""

import json
import os
import shutil

from runtime import log


def load_config(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "config.json")

    with open("config.example.json") as fh:
        defaults = json.load(fh)

    if not os.path.exists(path):
        shutil.copy("config.example.json", path)
        log("info", "Seeded config.json from config.example.json", path=path)
        return defaults

    with open(path) as fh:
        live = json.load(fh)

    missing = sorted(k for k in defaults if k not in live)
    if missing:
        log("warn", "config.json is missing keys - falling back to committed defaults",
            path=path, missing=missing)

    stale = sorted(k for k in live if k not in defaults)
    if stale:
        log("info", "config.json has keys this version ignores", path=path, stale=stale)

    return {**defaults, **live}
