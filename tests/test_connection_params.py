"""Tests for per-datasource connection_params — MySQL SSL disabled by default."""

import sys
import types

import pytest

from wren_agent.models.task import SourceDatabase
from wren_agent.tools.db_explorer import DBConnector


def _mysql_connector(**params) -> DBConnector:
    return DBConnector(
        host="h", port=3306, user="u", password="p",
        database="d", db_type="mysql", connection_params=params or None,
    )


@pytest.fixture
def fake_pymysql(monkeypatch):
    """Stub pymysql so _connect captures kwargs without a real connection."""
    captured = {}
    mod = types.ModuleType("pymysql")

    def connect(**kwargs):
        captured.update(kwargs)
        return object()

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "pymysql", mod)
    return captured


class TestMySQLSSLDefaults:
    def test_ssl_disabled_by_default(self, fake_pymysql):
        _mysql_connector()._connect()
        assert fake_pymysql["ssl_disabled"] is True
        assert "ssl" not in fake_pymysql

    def test_use_ssl_opt_in_builds_ssl_dict(self, fake_pymysql):
        _mysql_connector(use_ssl=True)._connect()
        assert fake_pymysql["ssl_disabled"] is False
        # pymysql only creates a TLS context when an `ssl` dict is passed
        assert fake_pymysql["ssl"] == {
            "ca": None, "check_hostname": False, "verify_mode": False,
        }

    def test_ssl_verify_options(self, fake_pymysql):
        _mysql_connector(
            use_ssl=True, ssl_ca="/certs/ca.pem",
            ssl_verify_cert=True, ssl_verify_identity=True,
        )._connect()
        assert fake_pymysql["ssl"] == {
            "ca": "/certs/ca.pem", "check_hostname": True, "verify_mode": True,
        }

    def test_charset_and_timeout_overrides(self, fake_pymysql):
        _mysql_connector(charset="gbk", connect_timeout=5)._connect()
        assert fake_pymysql["charset"] == "gbk"
        assert fake_pymysql["connect_timeout"] == 5
        assert fake_pymysql["read_timeout"] == 30  # default retained


class TestParseConnectionParams:
    @pytest.mark.parametrize("raw,expected", [
        ({"use_ssl": True}, {"use_ssl": True}),      # dict passthrough
        ('{"use_ssl": false}', {"use_ssl": False}),  # asyncpg jsonb-as-str
        ("", {}),
        (None, {}),
        ("not json", {}),
        ("[1,2]", {}),                               # non-dict json
    ])
    def test_parse(self, raw, expected):
        from wren_agent.tools.pg_store import parse_connection_params
        assert parse_connection_params(raw) == expected


class TestParamsFlow:
    @pytest.mark.asyncio
    async def test_registry_params_reach_extract_db_params(self, monkeypatch):
        from wren_agent.nodes import explore

        async def fake_lookup(key):
            return {"user": "root", "password": "pw", "db_type": "mysql",
                    "host": "h", "port": 1, "database": "d", "id": "x",
                    "name": "reg_db", "connection_params": {"use_ssl": False}}

        monkeypatch.setattr(
            "wren_agent.tools.pg_store.get_source_db_credentials", fake_lookup
        )

        sdb = SourceDatabase(
            name="reg_db", db_type="mysql", host="h", port=1,
            database="d", mcp_server_name="m",
        )
        params = await explore._extract_db_params(sdb)
        assert params["connection_params"] == {"use_ssl": False}
        assert params["credential_source"] == "registry"

    def test_source_database_model_accepts_params(self):
        sdb = SourceDatabase(
            name="n", db_type="mysql", host="h", port=1,
            database="d", mcp_server_name="m",
            connection_params={"use_ssl": True},
        )
        assert sdb.connection_params == {"use_ssl": True}

    def test_build_source_databases_passes_params(self):
        from wren_agent.api.routes import _build_source_databases

        dbs = _build_source_databases([{
            "name": "n", "db_type": "mysql", "host": "h", "port": 1,
            "database": "d", "connection_params": {"use_ssl": True},
        }])
        assert dbs[0].connection_params == {"use_ssl": True}
