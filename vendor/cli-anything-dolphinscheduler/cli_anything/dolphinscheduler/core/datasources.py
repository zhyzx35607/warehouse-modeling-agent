"""Datasource operations for DolphinScheduler.

Datasource definitions are stored server-side and referenced by SQL tasks via
their numeric ``id``. This module keeps the CLI thin: callers pass the same JSON
object that DolphinScheduler's UI sends to the REST API, and the server performs
plugin-specific validation.
"""

from __future__ import annotations

from typing import Any, Optional

from .client import DolphinSchedulerClient

_DATASOURCES = "/datasources"


def create_datasource(client: DolphinSchedulerClient, data_source_param: dict[str, Any]) -> Any:
    """Create a datasource from a DolphinScheduler datasource-param JSON object."""
    return client.post(_DATASOURCES, json_body=data_source_param)


def update_datasource(client: DolphinSchedulerClient, datasource_id: int, data_source_param: dict[str, Any]) -> Any:
    """Update an existing datasource by id."""
    payload = dict(data_source_param)
    payload["id"] = datasource_id
    return client.put(f"{_DATASOURCES}/{datasource_id}", json_body=payload)


def get_datasource(client: DolphinSchedulerClient, datasource_id: int) -> Any:
    """Fetch datasource detail by id."""
    return client.get(f"{_DATASOURCES}/{datasource_id}")


def list_datasources(
    client: DolphinSchedulerClient,
    *,
    search_val: Optional[str] = None,
    page_no: int = 1,
    page_size: int = 20,
) -> Any:
    """Return one page of visible datasources."""
    return client.get(
        _DATASOURCES,
        params={"searchVal": search_val, "pageNo": page_no, "pageSize": page_size},
    )


def list_datasources_by_type(client: DolphinSchedulerClient, datasource_type: str) -> Any:
    """Return visible datasources of one DbType, e.g. MYSQL or POSTGRESQL."""
    return client.get(f"{_DATASOURCES}/list", params={"type": datasource_type})


def test_datasource_param(client: DolphinSchedulerClient, data_source_param: dict[str, Any]) -> Any:
    """Test connection parameters before creating or updating a datasource."""
    return client.post(f"{_DATASOURCES}/connect", json_body=data_source_param)


def test_datasource(client: DolphinSchedulerClient, datasource_id: int) -> Any:
    """Test an existing datasource connection."""
    return client.get(f"{_DATASOURCES}/{datasource_id}/connect-test")


def delete_datasource(client: DolphinSchedulerClient, datasource_id: int) -> Any:
    """Delete a datasource by id."""
    return client.delete(f"{_DATASOURCES}/{datasource_id}")


def verify_name(client: DolphinSchedulerClient, name: str) -> Any:
    """Ask the server whether a datasource name is available."""
    return client.get(f"{_DATASOURCES}/verify-name", params={"name": name})


def kerberos_startup_state(client: DolphinSchedulerClient) -> Any:
    """Return the server's Kerberos startup state for datasource/resource usage."""
    return client.get(f"{_DATASOURCES}/kerberos-startup-state")


def list_databases(client: DolphinSchedulerClient, datasource_id: int) -> Any:
    """List databases visible through a datasource."""
    return client.get(f"{_DATASOURCES}/databases", params={"datasourceId": datasource_id})


def list_tables(client: DolphinSchedulerClient, datasource_id: int, database: str) -> Any:
    """List tables for a database in a datasource."""
    return client.get(
        f"{_DATASOURCES}/tables",
        params={"datasourceId": datasource_id, "database": database},
    )


def list_table_columns(
    client: DolphinSchedulerClient,
    datasource_id: int,
    database: str,
    table_name: str,
) -> Any:
    """List columns for one table in a datasource database."""
    return client.get(
        f"{_DATASOURCES}/tableColumns",
        params={"datasourceId": datasource_id, "database": database, "tableName": table_name},
    )
