"""Unit tests use local placeholders and must never contact external services."""
import socket
import inspect
import pytest


@pytest.fixture(autouse=True)
def isolated_unit_settings(monkeypatch):
    from wren_agent.config import settings
    monkeypatch.setattr(settings.llm, 'allow_programmatic_fallback', True)
    monkeypatch.setattr(settings.llm, 'api_key', 'unit-test-placeholder')
    monkeypatch.setattr(settings.llm, 'api_base', 'http://127.0.0.1:1/v1')
    monkeypatch.setattr(settings.llm, 'request_timeout', 1)
    monkeypatch.setattr(settings.llm, 'max_retries', 0)
    monkeypatch.setattr(settings.sm4, 'key', '00000000000000000000000000000000')
    original_connect = socket.socket.connect
    def blocked(self, address):
        if inspect.currentframe().f_back.f_code.co_name == '_fallback_socketpair':
            return original_connect(self, address)
        raise AssertionError('Unit tests must mock network adapters')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
