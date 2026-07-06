"""Shared test setup.

transport.py (lazily) and main.py (at import) reference ``mqttrpc``, which is not
installed in the test / CI-build environment. Provide a stub with the exception
classes the code catches so pure-logic tests can import and exercise the modules
with fakes. If the real package IS present (e.g. on a controller) it is left in
place — the tests construct the same classes the code catches either way.
"""

import sys
import types

if "mqttrpc" not in sys.modules:
    try:
        import mqttrpc  # noqa: F401  (use the real package if available)
    except ImportError:
        _client = types.ModuleType("mqttrpc.client")

        class TimeoutError(Exception):  # noqa: A001 - mirrors mqttrpc.client.TimeoutError
            pass

        class MQTTRPCError(Exception):
            def __init__(self, message="", code=0, data=None):
                super().__init__(message)
                self.code = code
                self.data = data

        class TMQTTRPCClient:  # referenced only at runtime in main(), never in tests
            def __init__(self, client):
                self.client = client
                self.subscribes = set()

        _client.TimeoutError = TimeoutError
        _client.MQTTRPCError = MQTTRPCError
        _client.TMQTTRPCClient = TMQTTRPCClient
        _mqttrpc = types.ModuleType("mqttrpc")
        _mqttrpc.client = _client
        sys.modules["mqttrpc"] = _mqttrpc
        sys.modules["mqttrpc.client"] = _client
