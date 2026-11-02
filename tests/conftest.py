"""Stubs for the secret reader and the plugin data directory."""
import sys
import types

import pytest

import client
import prefix
import watch
from tests.state import STATE, UnscopedSecretError, get_secret


def _install() -> None:
    agent = types.ModuleType("agent")
    secret = types.ModuleType("agent.secret_scope")
    secret.UnscopedSecretError = UnscopedSecretError
    secret.get_secret = get_secret
    agent.secret_scope = secret
    sys.modules["agent"] = agent
    sys.modules["agent.secret_scope"] = secret
    plugins = types.ModuleType("plugins")
    storage = types.ModuleType("plugins.plugin_storage")

    def plugin_data_dir(name):
        path = STATE["data_dir"]
        if path is None:
            raise RuntimeError("data dir unset")
        path.mkdir(parents=True, exist_ok=True)
        return path

    storage.plugin_data_dir = plugin_data_dir
    plugins.plugin_storage = storage
    sys.modules["plugins"] = plugins
    sys.modules["plugins.plugin_storage"] = storage


def _refuse_live(self, method, url, headers, timeout):
    raise AssertionError("live HTTP was attempted: " + url.split("?", 1)[0])


client.REAL_URLLIB_SEND = client.UrllibTransport.send
client.UrllibTransport.send = _refuse_live

_install()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def runtime():
    clock = Clock()
    client.reset_runtime()
    client.configure(clock=clock, sleep=clock.sleep)
    STATE["values"].clear()
    STATE["raise"] = None
    STATE["calls"].clear()
    STATE["data_dir"] = None
    watch.set_data_dir(None)
    prefix.remember_profile_name(None)
    yield clock
    client.reset_runtime()
    watch.set_data_dir(None)
    prefix.remember_profile_name(None)
