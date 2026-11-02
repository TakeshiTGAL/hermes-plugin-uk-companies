"""Shared secret stub state. Both conftest and the tests use this dict."""

STATE = {"values": {}, "raise": None, "calls": [], "data_dir": None}


class UnscopedSecretError(Exception):
    pass


def get_secret(name, default=None):
    STATE["calls"].append(name)
    if STATE["raise"] == "unscoped":
        raise UnscopedSecretError("sentinel-unscoped-SECRETVALUE12")
    if STATE["raise"] == "boom":
        raise RuntimeError("sentinel-boom-SECRETVALUE12")
    if name in STATE["values"]:
        return STATE["values"][name]
    return default
