"""Build stable source catalogs and classify schema compatibility."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from wren_agent.models.schema import SchemaExploration
from wren_agent.models.stage import (
    CatalogChange,
    CatalogChangeKind,
    CatalogColumn,
    CatalogDiff,
    CatalogTable,
    SourceCatalog,
)


def _normalize_type(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _type_shape(value: str) -> tuple[str, tuple[int, ...]]:
    normalized = _normalize_type(value)
    match = re.fullmatch(r"([A-Z ]+?)(?:\((\d+)(?:,(\d+))?\))?", normalized)
    if not match:
        return normalized, ()
    return match.group(1), tuple(int(v) for v in match.groups()[1:] if v is not None)


def _type_relation(previous: str, current: str) -> str:
    """Return equal, widened, or narrowed/changed conservatively."""
    left, left_args = _type_shape(previous)
    right, right_args = _type_shape(current)
    if (left, left_args) == (right, right_args):
        return "equal"
    if left == right and left_args and right_args and len(left_args) == len(right_args):
        return "widened" if all(a <= b for a, b in zip(left_args, right_args)) else "narrowed"
    integer_rank = {"TINYINT": 1, "SMALLINT": 2, "INT": 3, "INTEGER": 3, "BIGINT": 4}
    if left in integer_rank and right in integer_rank:
        return "widened" if integer_rank[left] < integer_rank[right] else "narrowed"
    return "narrowed"


def compute_catalog_fingerprint(tables: Iterable[CatalogTable]) -> str:
    """Hash structural fields only; comments and profiling data do not churn it."""
    structural = []
    for table in sorted(tables, key=lambda item: item.identity):
        structural.append({
            "identity": table.identity,
            "name": [table.schema_name, table.table_name],
            "columns": [{
                "id": column.source_column_id,
                "name": column.name,
                "type": _normalize_type(column.data_type),
                "ordinal": column.ordinal,
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "incremental_key": column.incremental_key,
            } for column in sorted(table.columns, key=lambda item: item.ordinal)],
        })
    encoded = json.dumps(structural, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_source_catalog(explorations: dict[str, SchemaExploration]) -> SourceCatalog:
    tables: list[CatalogTable] = []
    for source_db_id, exploration in sorted(explorations.items()):
        for table in exploration.tables:
            tables.append(CatalogTable(
                source_db_id=source_db_id,
                schema_name=table.schema_name,
                table_name=table.name,
                columns=[CatalogColumn(
                    name=field.name,
                    data_type=field.data_type,
                    ordinal=index,
                    nullable=field.nullable,
                    primary_key=field.primary_key,
                    incremental_key=field.is_incremental_key,
                    comment=field.comment or "",
                ) for index, field in enumerate(table.fields)],
            ))
    return SourceCatalog(tables=tables, schema_fingerprint=compute_catalog_fingerprint(tables))


def diff_source_catalog(previous: SourceCatalog, current: SourceCatalog) -> CatalogDiff:
    changes: list[CatalogChange] = []
    old_tables = {table.identity: table for table in previous.tables}
    new_tables = {table.identity: table for table in current.tables}

    def add(kind, table_id, column="", before=None, after=None, invalidates=False):
        changes.append(CatalogChange(kind=kind, table_identity=table_id, column=column,
                                     previous=before, current=after, invalidates_ods=invalidates))

    for table_id in sorted(old_tables.keys() - new_tables.keys()):
        add(CatalogChangeKind.TABLE_REMOVED, table_id, invalidates=True)
    for table_id in sorted(new_tables.keys() - old_tables.keys()):
        add(CatalogChangeKind.TABLE_ADDED, table_id)

    for table_id in sorted(old_tables.keys() & new_tables.keys()):
        old = old_tables[table_id]
        new = new_tables[table_id]
        old_by_id = {c.source_column_id: c for c in old.columns if c.source_column_id}
        new_by_id = {c.source_column_id: c for c in new.columns if c.source_column_id}
        old_named = {c.name: c for c in old.columns}
        new_named = {c.name: c for c in new.columns}

        matched: list[tuple[CatalogColumn, CatalogColumn]] = []
        consumed_old: set[str] = set()
        consumed_new: set[str] = set()
        for column_id in old_by_id.keys() & new_by_id.keys():
            before, after = old_by_id[column_id], new_by_id[column_id]
            matched.append((before, after))
            consumed_old.add(before.name)
            consumed_new.add(after.name)
            if before.name != after.name:
                add(CatalogChangeKind.COLUMN_RENAMED, table_id, after.name,
                    before.name, after.name, True)
        for name in old_named.keys() & new_named.keys():
            if name not in consumed_old and name not in consumed_new:
                matched.append((old_named[name], new_named[name]))
                consumed_old.add(name)
                consumed_new.add(name)

        removed = [c for c in old.columns if c.name not in consumed_old]
        added = [c for c in new.columns if c.name not in consumed_new]
        # Without native IDs, same ordinal + compatible type is a suspected rename.
        for before in list(removed):
            candidate = next((after for after in added
                              if after.ordinal == before.ordinal
                              and _type_relation(before.data_type, after.data_type) != "narrowed"), None)
            if candidate:
                add(CatalogChangeKind.COLUMN_RENAMED, table_id, candidate.name,
                    before.name, candidate.name, True)
                matched.append((before, candidate))
                removed.remove(before)
                added.remove(candidate)
        for column in removed:
            add(CatalogChangeKind.COLUMN_REMOVED, table_id, column.name, column.model_dump(), None, True)
        for column in added:
            add(CatalogChangeKind.COLUMN_ADDED, table_id, column.name, None, column.model_dump())

        for before, after in matched:
            relation = _type_relation(before.data_type, after.data_type)
            if relation == "widened":
                add(CatalogChangeKind.TYPE_WIDENED, table_id, after.name,
                    before.data_type, after.data_type)
            elif relation == "narrowed":
                add(CatalogChangeKind.TYPE_NARROWED, table_id, after.name,
                    before.data_type, after.data_type, True)
            if before.primary_key != after.primary_key:
                add(CatalogChangeKind.KEY_CHANGED, table_id, after.name,
                    before.primary_key, after.primary_key, True)
            if before.incremental_key != after.incremental_key:
                add(CatalogChangeKind.INCREMENTAL_KEY_CHANGED, table_id, after.name,
                    before.incremental_key, after.incremental_key, True)
            if before.nullable and not after.nullable:
                add(CatalogChangeKind.NULLABILITY_TIGHTENED, table_id, after.name, True, False, True)
            if before.comment != after.comment:
                add(CatalogChangeKind.COMMENT_CHANGED, table_id, after.name,
                    before.comment, after.comment)

    invalidated = sorted({change.table_identity for change in changes if change.invalidates_ods})
    return CatalogDiff(compatible=not invalidated, changes=changes, invalidated_tables=invalidated)
