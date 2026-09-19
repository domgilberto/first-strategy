"""
Non-secret tunables.

config.json lives on the persistent volume and is only seeded when absent, so a
config written by an earlier version of this strategy survives a deploy and can
be missing keys the new code expects - at the top level or inside a section.
Merge it over the committed defaults, section by section, rather than trusting
it to be complete, and say what was missing.
"""

import json
import os
import shutil

from runtime import log


def _deep_merge(defaults, live):
    """Return defaults overlaid with live, recursing into dict sections so a
    partially-populated section keeps the defaults for the keys it lacks."""
    out = dict(defaults)
    for key, value in live.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            out[key] = _deep_merge(defaults[key], value)
        else:
            out[key] = value
    return out


def _missing(defaults, live, prefix=""):
    missing = []
    for key, value in defaults.items():
        if key not in live:
            missing.append(prefix + key)
        elif isinstance(value, dict) and isinstance(live.get(key), dict):
            missing += _missing(value, live[key], prefix + key + ".")
    return sorted(missing)


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

    missing = _missing(defaults, live)
    if missing:
        log("warn", "config.json is missing keys - falling back to committed defaults",
            path=path, missing=missing)

    stale = sorted(k for k in live if k not in defaults)
    if stale:
        log("info", "config.json has keys this version ignores", path=path, stale=stale)

    return _deep_merge(defaults, live)
