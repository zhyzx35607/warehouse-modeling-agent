"""SourceCatalog fingerprint and compatibility policy tests."""

from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.models.stage import CatalogColumn, CatalogTable, SourceCatalog
from wren_agent.tools.source_catalog import (
    build_source_catalog,
    compute_catalog_fingerprint,
    diff_source_catalog,
)


def _table(*columns: CatalogColumn) -> CatalogTable:
    return CatalogTable(source_db_id="db-1", schema_name="public",
                        table_name="orders", columns=list(columns))


def _column(name="id", dtype="INT", ordinal=0, **kwargs) -> CatalogColumn:
    return CatalogColumn(name=name, data_type=dtype, ordinal=ordinal, **kwargs)


def test_fingerprint_ignores_comments_but_tracks_structure():
    first = _table(_column(comment="old"))
    comment_only = _table(_column(comment="new"))
    widened = _table(_column(dtype="BIGINT", comment="new"))
    assert compute_catalog_fingerprint([first]) == compute_catalog_fingerprint([comment_only])
    assert compute_catalog_fingerprint([first]) != compute_catalog_fingerprint([widened])


def test_build_catalog_excludes_profile_values():
    exploration = SchemaExploration(source_db_id="db-1", tables=[TableSchema(
        name="orders", schema_name="sales", db_type="mysql", row_count_estimate=999,
        fields=[FieldSchema(name="id", data_type="INT", sample_values=[123],
                            distinct_count=1, primary_key=True)],
    )])
    catalog = build_source_catalog({"db-1": exploration})
    assert catalog.tables[0].columns[0].primary_key is True
    assert "sample_values" not in catalog.tables[0].columns[0].model_dump()
    assert catalog.schema_fingerprint


def test_compatible_changes_do_not_invalidate_ods():
    before = SourceCatalog(tables=[_table(
        _column(dtype="VARCHAR(20)", comment="old"),
    )])
    after = SourceCatalog(tables=[_table(
        _column(dtype="VARCHAR(50)", comment="new"),
        _column("note", "TEXT", 1),
    )])
    diff = diff_source_catalog(before, after)
    assert diff.compatible is True
    assert diff.invalidated_tables == []
    assert {change.kind.value for change in diff.changes} == {
        "type_widened", "comment_changed", "column_added",
    }


def test_breaking_changes_invalidate_only_affected_table():
    before = SourceCatalog(tables=[_table(
        _column(dtype="BIGINT", primary_key=True, incremental_key=True),
    )])
    after = SourceCatalog(tables=[_table(
        _column(dtype="INT", primary_key=False, incremental_key=False),
    )])
    diff = diff_source_catalog(before, after)
    assert diff.compatible is False
    assert diff.invalidated_tables == ["db-1:public.orders"]
    assert {change.kind.value for change in diff.changes} == {
        "type_narrowed", "key_changed", "incremental_key_changed",
    }


def test_stable_column_id_detects_rename_as_breaking():
    before = SourceCatalog(tables=[_table(
        _column("customer_id", source_column_id="col-7"),
    )])
    after = SourceCatalog(tables=[_table(
        _column("buyer_id", source_column_id="col-7"),
    )])
    diff = diff_source_catalog(before, after)
    assert diff.compatible is False
    assert [change.kind.value for change in diff.changes] == ["column_renamed"]


def test_suspected_rename_without_native_id_is_breaking():
    before = SourceCatalog(tables=[_table(_column("customer_id"))])
    after = SourceCatalog(tables=[_table(_column("buyer_id"))])
    diff = diff_source_catalog(before, after)
    assert diff.compatible is False
    assert diff.changes[0].kind.value == "column_renamed"
