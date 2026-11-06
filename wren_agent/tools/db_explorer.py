"""Database exploration tools — real MySQL/PostgreSQL connection.

Direct database connections for MCP-style exploration.
Supports MySQL, PostgreSQL, with type mapping to Doris.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import threading
from typing import Any, Optional

from wren_agent.models.schema import (
    FieldSchema,
    RelationshipType,
    SchemaExploration,
    SchemaSnapshot,
    TableRelationship,
    TableSchema,
)
from wren_agent.models.task import SensorResult, SensorType

logger = logging.getLogger(__name__)

# ── Type mapping: source DB type → Doris type ───────────────────

TYPE_MAPPING: dict[str, dict[str, Optional[str]]] = {
    "mysql": {
        "TINYINT": "TINYINT", "SMALLINT": "SMALLINT", "MEDIUMINT": "INT",
        "INT": "INT", "INTEGER": "INT", "BIGINT": "BIGINT",
        "FLOAT": "FLOAT", "DOUBLE": "DOUBLE", "DOUBLE PRECISION": "DOUBLE",
        "DECIMAL": "DECIMAL", "NUMERIC": "DECIMAL",
        "VARCHAR": "VARCHAR", "CHAR": "CHAR",
        "TEXT": "STRING", "TINYTEXT": "STRING", "MEDIUMTEXT": "STRING", "LONGTEXT": "STRING",
        "DATE": "DATE", "DATETIME": "DATETIME", "TIMESTAMP": "DATETIME", "TIME": "STRING",
        "JSON": "JSON", "BLOB": "STRING", "ENUM": "STRING", "SET": "STRING",
        "GEOMETRY": None, "POINT": None, "GEOMETRYCOLLECTION": None,
    },
    "postgresql": {
        "INTEGER": "INT", "INT": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
        "REAL": "FLOAT", "DOUBLE PRECISION": "DOUBLE",
        "NUMERIC": "DECIMAL", "DECIMAL": "DECIMAL",
        "VARCHAR": "VARCHAR", "CHARACTER VARYING": "VARCHAR",
        "CHAR": "CHAR", "CHARACTER": "CHAR",
        "TEXT": "STRING", "DATE": "DATE",
        "TIMESTAMP": "DATETIME", "TIMESTAMPTZ": "DATETIME",
        "BOOLEAN": "BOOLEAN", "BOOL": "BOOLEAN",
        "JSON": "JSON", "JSONB": "JSON",
        "UUID": "VARCHAR(36)",
        "BYTEA": "STRING",
        "GEOMETRY": None,
    },
    "oracle": {
        "NUMBER": "DECIMAL", "INTEGER": "BIGINT",
        "VARCHAR2": "VARCHAR", "VARCHAR": "VARCHAR",
        "CHAR": "CHAR", "NVARCHAR2": "VARCHAR",
        "CLOB": "STRING", "NCLOB": "STRING", "LONG": "STRING",
        "DATE": "DATETIME", "TIMESTAMP": "DATETIME",
        "TIMESTAMP WITH TIME ZONE": "DATETIME",
        "BLOB": "STRING", "RAW": "STRING",
        "FLOAT": "DOUBLE", "BINARY_FLOAT": "FLOAT", "BINARY_DOUBLE": "DOUBLE",
        "SDO_GEOMETRY": None,
        "XMLTYPE": "STRING",
    },
    "dm": {
        "INT": "INT", "INTEGER": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT", "TINYINT": "TINYINT",
        "NUMERIC": "DECIMAL", "NUMBER": "DECIMAL", "DECIMAL": "DECIMAL", "DEC": "DECIMAL",
        "FLOAT": "DOUBLE", "REAL": "FLOAT", "DOUBLE": "DOUBLE", "DOUBLE PRECISION": "DOUBLE",
        "VARCHAR": "VARCHAR", "VARCHAR2": "VARCHAR", "CHAR": "CHAR", "CHARACTER": "CHAR",
        "TEXT": "STRING", "CLOB": "STRING", "LONGVARCHAR": "STRING",
        "DATE": "DATETIME", "TIME": "STRING", "TIMESTAMP": "DATETIME",
        "DATETIME": "DATETIME",
        "BLOB": "STRING", "IMAGE": "STRING", "BINARY": "STRING", "VARBINARY": "STRING",
        "BIT": "BOOLEAN", "BOOLEAN": "BOOLEAN",
        "INTERVAL": None,
    },
    "kingbase": {
        "INTEGER": "INT", "INT": "INT", "BIGINT": "BIGINT", "SMALLINT": "SMALLINT",
        "REAL": "FLOAT", "DOUBLE PRECISION": "DOUBLE",
        "NUMERIC": "DECIMAL", "DECIMAL": "DECIMAL",
        "VARCHAR": "VARCHAR", "CHARACTER VARYING": "VARCHAR",
        "CHAR": "CHAR", "CHARACTER": "CHAR",
        "TEXT": "STRING", "DATE": "DATE",
        "TIMESTAMP": "DATETIME", "TIMESTAMPTZ": "DATETIME",
        "BOOLEAN": "BOOLEAN",
        "JSON": "JSON", "JSONB": "JSON",
        "UUID": "VARCHAR(36)",
        "BYTEA": "STRING",
        "GEOMETRY": None,
    },
}


def map_type_to_doris(source_type: str, db_type: str = "mysql") -> tuple[Optional[str], bool]:
    """Map source DB column type to Doris type. Returns (doris_type, is_unsupported)."""
    mapping = TYPE_MAPPING.get(db_type.lower(), {})
    base_type = source_type.upper().split("(")[0].strip()
    doris_type = mapping.get(base_type, "STRING")
    is_unsupported = doris_type is None
    return doris_type if not is_unsupported else None, is_unsupported


# ── Sensitive field detection ───────────────────────────────────

SENSITIVE_PATTERNS: list[dict] = [
    {"type": SensorType.PHONE, "patterns": [r"^(phone|mobile|tel)", r"手机号?", r"联系电话"]},
    {"type": SensorType.ID_CARD, "patterns": [r"^(id_card|identity|sfz|身份证|cert_no)"]},
    {"type": SensorType.BANK_CARD, "patterns": [r"^(bank_card|bankcard|bank_account|银行卡)"]},
    {"type": SensorType.EMAIL, "patterns": [r"^(email|e_mail|mail|邮箱)"]},
    {"type": SensorType.ADDRESS, "patterns": [r"^(address|addr|location|地址|住址)"]},
    {"type": SensorType.NAME, "patterns": [r"^(name|姓名|real_name|nickname)"]},
    {"type": SensorType.PASSWORD, "patterns": [r"^(password|passwd|pwd|pin|secret|密码)"]},
    {"type": SensorType.IP, "patterns": [r"^(ip_addr|client_ip|server_ip|remote_ip)"]},
]


def detect_sensitive_field(table_name: str, field_name: str) -> Optional[SensorResult]:
    for entry in SENSITIVE_PATTERNS:
        for pattern in entry["patterns"]:
            if re.search(pattern, field_name, re.IGNORECASE):
                return SensorResult(
                    table_name=table_name,
                    field_name=field_name,
                    sensor_type=entry["type"],
                    confidence=0.9,
                )
    return None


def apply_safe_samples(
    table_schema: TableSchema,
    sample_rows: list[dict],
    max_values: int = 3,
) -> None:
    """Attach samples only to fields that are not classified as sensitive.

    Classification happens before any value is copied into the schema model so
    LangGraph checkpoints and downstream prompts never receive sensitive sample
    values. Existing ``is_sensitive`` flags are honored for classifiers outside
    the built-in name-pattern detector.
    """
    for field in table_schema.fields:
        detected = detect_sensitive_field(table_schema.name, field.name)
        if field.is_sensitive or detected is not None:
            field.is_sensitive = True
            field.sample_values = []
            continue

        field.sample_values = [
            row.get(field.name)
            for row in sample_rows[:max_values]
            if field.name in row and row.get(field.name) is not None
        ]


def safe_sample_columns(table_schema: TableSchema) -> list[str]:
    """Return sampleable columns after applying sensitivity classification."""
    safe_columns: list[str] = []
    for field in table_schema.fields:
        if field.is_sensitive or detect_sensitive_field(table_schema.name, field.name):
            field.is_sensitive = True
            field.sample_values = []
        else:
            safe_columns.append(field.name)
    return safe_columns


# ── DB Connector ────────────────────────────────────────────────


class DBConnector:
    """Direct database connector for exploration operations."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        db_type: str = "mysql",
        connection_params: dict | None = None,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.db_type = db_type
        # Driver-level options from the datasource registry
        # (e.g. {"use_ssl": false, "charset": "utf8mb4", "connect_timeout": 15})
        self.connection_params = connection_params or {}
        # Reuse one connection across metadata/sample queries for this connector
        # lifetime (explore path previously opened a TCP session per query).
        self._conn: Any = None
        self._conn_lock = threading.Lock()

    def close(self) -> None:
        """Close the pooled connection if open."""
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    async def aclose(self) -> None:
        """Async wrapper around close() for node cleanup."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.close)

    def _connect(self):
        """Create a new connection — dynamically selects driver by db_type.

        New DB types only need a new elif branch here.
        Agent code is never touched when adding database support.

        Supported: mysql (pymysql), postgresql (pg8000),
                   oracle (cx_Oracle), dm (dmPython),
                   kingbase (ksql), mongodb (pymongo),
                   doris (pymysql — MySQL wire protocol on the query port).
        """
        params = self.connection_params

        if self.db_type in ("mysql", "doris"):
            import pymysql
            # SSL is disabled by default — many on-prem MySQL instances have
            # broken/self-signed certs; set connection_params.use_ssl=true to opt in.
            # NOTE: pymysql only builds an SSL context when an `ssl` dict is
            # passed — flag-style kwargs alone silently skip TLS.
            if bool(params.get("use_ssl", False)):
                ssl_kwargs = {
                    "ssl_disabled": False,
                    "ssl": {
                        "ca": params.get("ssl_ca"),
                        "check_hostname": bool(params.get("ssl_verify_identity", False)),
                        "verify_mode": bool(params.get("ssl_verify_cert", False)),
                    },
                }
            else:
                ssl_kwargs = {"ssl_disabled": True}
            return pymysql.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                database=self.database,
                charset=params.get("charset", "utf8mb4"),
                connect_timeout=params.get("connect_timeout", 15),
                read_timeout=params.get("read_timeout", 30),
                **ssl_kwargs,
            )

        elif self.db_type == "postgresql":
            import pg8000
            return pg8000.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                database=self.database,
                timeout=15,
            )

        elif self.db_type == "oracle":
            import cx_Oracle
            dsn = cx_Oracle.makedsn(self.host, self.port, service_name=self.database)
            return cx_Oracle.connect(
                user=self.user, password=self.password, dsn=dsn,
            )

        elif self.db_type == "dm":
            # 武汉达梦数据库
            import dmPython
            return dmPython.connect(
                server=self.host, port=self.port,
                user=self.user, password=self.password,
                schema=self.database,
            )

        elif self.db_type == "kingbase":
            # 人大金仓 (基于 PostgreSQL 协议，pg8000 兼容)
            import pg8000
            return pg8000.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                database=self.database,
                timeout=15,
            )

        elif self.db_type == "mongodb":
            from pymongo import MongoClient
            conn_str = f"mongodb://{self.user}:{self.password}@{self.host}:{self.port}"
            if self.database:
                conn_str += f"/{self.database}"
            return MongoClient(conn_str, serverSelectionTimeoutMS=15000)

        else:
            raise ValueError(
                f"Unsupported db_type: '{self.db_type}'. "
                f"Supported: mysql, postgresql, oracle, dm, kingbase, mongodb"
            )

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read query and return rows as dicts.

        Reuses a single connection under a lock. On query failure the pooled
        connection is dropped so the next call opens a fresh one.
        """

        def _run():
            with self._conn_lock:
                conn = self._conn
                if conn is None:
                    conn = self._connect()
                    self._conn = conn
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        columns = (
                            [col[0] for col in cur.description]
                            if cur.description else []
                        )
                        rows = cur.fetchall()
                        return [dict(zip(columns, row)) for row in rows]
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    raise

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)

    async def query_read_only(
        self,
        sql: str,
        params: tuple = (),
        max_rows: int = 500,
    ) -> tuple[list[dict], bool]:
        """Execute user-supplied SQL in a DB-enforced read-only transaction.

        This is intentionally separate from internal metadata queries. If the
        source driver cannot establish a read-only transaction, execution fails
        closed instead of silently relying on SQL text validation alone.
        """
        if self.db_type not in ("mysql", "postgresql", "kingbase", "oracle", "dm"):
            raise ValueError(
                f"Read-only arbitrary queries are not supported for {self.db_type}"
            )

        def _run():
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(sql, params)
                    columns = [col[0] for col in cur.description] if cur.description else []
                    raw_rows = cur.fetchmany(max_rows + 1)
                    rows = [dict(zip(columns, row)) for row in raw_rows[:max_rows]]
                    return rows, len(raw_rows) > max_rows
            finally:
                try:
                    conn.rollback()
                finally:
                    conn.close()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)

    async def query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Execute a query and return the first row."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def query_value(self, sql: str, params: tuple = ()) -> Any:
        """Execute a query and return a single scalar value."""
        row = await self.query_one(sql, params)
        if row:
            return list(row.values())[0]
        return None

    # ── Schema exploration ──────────────────────────────────

    async def list_tables(self) -> list[str]:
        """List all tables in the database."""
        if self.db_type in ("mysql", "doris"):
            rows = await self.query(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (self.database,),
            )
            return [r["TABLE_NAME"] for r in rows]
        if self.db_type in ("postgresql", "kingbase"):
            rows = await self.query(
                "SELECT tablename FROM pg_catalog.pg_tables "
                "WHERE schemaname = current_schema() ORDER BY tablename",
            )
            return [r["tablename"] for r in rows]
        return []

    async def list_databases(self) -> list[str]:
        """List all databases on the server (MySQL/Doris wire protocol).

        Used by warehouse-source auto-discovery: new databases created
        directly on the cluster are detected and classified into layers.
        """
        if self.db_type in ("mysql", "doris"):
            rows = await self.query("SHOW DATABASES")
            return [r["Database"] for r in rows]
        return []

    def _fields_from_column_rows(
        self, table_name: str, cols: list[dict],
    ) -> list[FieldSchema]:
        """Build FieldSchema list from information_schema / pg_catalog rows."""
        fields: list[FieldSchema] = []
        for c in cols:
            is_pk = c["COLUMN_KEY"] == "PRI"
            doris_type, unsupported = map_type_to_doris(c["COLUMN_TYPE"], self.db_type)
            fields.append(FieldSchema(
                name=c["COLUMN_NAME"],
                data_type=c["COLUMN_TYPE"],
                doris_type=doris_type if not unsupported else None,
                nullable=c["IS_NULLABLE"] == "YES",
                primary_key=is_pk,
                default_value=c["COLUMN_DEFAULT"],
                comment=c.get("COLUMN_COMMENT") or "",
                is_sensitive=detect_sensitive_field(table_name, c["COLUMN_NAME"]) is not None,
            ))
        return fields

    async def _row_estimates_for_tables(
        self, table_names: list[str],
    ) -> dict[str, Optional[int]]:
        """Fetch estimated row counts for many tables in as few queries as possible."""
        if not table_names:
            return {}
        wanted = set(table_names)
        out: dict[str, Optional[int]] = {n: None for n in table_names}

        if self.db_type == "doris":
            # Doris TABLE_ROWS is NULL; SHOW TABLE STATUS fills Rows (T-02/T-06).
            rows = await self.query(f"SHOW TABLE STATUS FROM `{self.database}`")
            for r in rows:
                name = r.get("Name") or r.get("name")
                if name in wanted and r.get("Rows") is not None:
                    out[name] = int(r["Rows"])
            return out

        if self.db_type in ("postgresql", "kingbase"):
            placeholders = ", ".join(["%s"] * len(table_names))
            rows = await self.query(
                "SELECT c.relname AS table_name, c.reltuples::bigint AS n "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname = current_schema() AND c.relname IN ({placeholders})",
                tuple(table_names),
            )
            for r in rows:
                if r.get("n") is not None:
                    out[r["table_name"]] = int(r["n"])
            return out

        # MySQL and other information_schema dialects
        placeholders = ", ".join(["%s"] * len(table_names))
        rows = await self.query(
            "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders})",
            (self.database, *table_names),
        )
        for r in rows:
            if r.get("TABLE_ROWS") is not None:
                out[r["TABLE_NAME"]] = int(r["TABLE_ROWS"])
        return out

    async def get_tables_info(self, table_names: list[str]) -> list[TableSchema]:
        """Batch-fetch column metadata + row estimates for many tables.

        One COLUMNS catalog query (plus one row-estimate query) instead of
        2N round-trips. Order of returned schemas follows ``table_names``.
        """
        if not table_names:
            return []

        # Preserve caller order; drop duplicates so IN-list stays small.
        ordered: list[str] = []
        seen: set[str] = set()
        for name in table_names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

        placeholders = ", ".join(["%s"] * len(ordered))
        if self.db_type in ("postgresql", "kingbase"):
            # PG-family: comments live in pg_catalog.col_description.
            cols = await self.query(
                'SELECT c.relname AS "TABLE_NAME", '
                'a.attname AS "COLUMN_NAME", '
                'pg_catalog.format_type(a.atttypid, a.atttypmod) AS "COLUMN_TYPE", '
                'CASE WHEN a.attnotnull THEN \'NO\' ELSE \'YES\' END AS "IS_NULLABLE", '
                'pg_get_expr(ad.adbin, ad.adrelid) AS "COLUMN_DEFAULT", '
                'CASE WHEN con.contype = \'p\' THEN \'PRI\' ELSE \'\' END AS "COLUMN_KEY", '
                'col_description(a.attrelid, a.attnum) AS "COLUMN_COMMENT", '
                'a.attnum AS "ORDINAL_POSITION" '
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_catalog.pg_attrdef ad "
                "  ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
                "LEFT JOIN pg_catalog.pg_constraint con "
                "  ON con.conrelid = a.attrelid AND a.attnum = ANY(con.conkey) "
                " AND con.contype = 'p' "
                f"WHERE n.nspname = current_schema() AND c.relname IN ({placeholders}) "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                'ORDER BY c.relname, a.attnum',
                tuple(ordered),
            )
        else:
            cols = await self.query(
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, "
                "COLUMN_DEFAULT, COLUMN_KEY, COLUMN_COMMENT, ORDINAL_POSITION "
                "FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders}) "
                "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (self.database, *ordered),
            )

        by_table: dict[str, list[dict]] = {n: [] for n in ordered}
        for c in cols:
            tname = c["TABLE_NAME"]
            if tname in by_table:
                by_table[tname].append(c)

        estimates = await self._row_estimates_for_tables(ordered)

        results: list[TableSchema] = []
        for table_name in ordered:
            table_cols = by_table.get(table_name) or []
            if not table_cols:
                logger.warning(
                    "No columns returned for %s.%s — skipping",
                    self.database, table_name,
                )
                continue
            est = estimates.get(table_name)
            results.append(TableSchema(
                name=table_name,
                schema_name=self.database,
                db_type=self.db_type,
                fields=self._fields_from_column_rows(table_name, table_cols),
                row_count_estimate=est,
            ))
        return results

    async def get_table_info(self, table_name: str) -> TableSchema:
        """Get table schema with column details, keys, and row estimates."""
        tables = await self.get_tables_info([table_name])
        if not tables:
            raise ValueError(
                f"Table not found or has no columns: {self.database}.{table_name}"
            )
        return tables[0]

    def _quote_identifier(self, identifier: str) -> str:
        """Quote a metadata-derived identifier for the current SQL dialect."""
        if "\x00" in identifier:
            raise ValueError("Database identifier contains a null byte")
        quote = "`" if self.db_type in ("mysql", "doris") else '"'
        return f"{quote}{identifier.replace(quote, quote * 2)}{quote}"

    async def sample_data(
        self,
        table_name: str,
        limit: int = 100,
        columns: list[str] | None = None,
    ) -> list[dict]:
        """Sample rows, optionally restricting the query to approved columns."""
        if columns == []:
            return []
        projection = "*" if columns is None else ", ".join(
            self._quote_identifier(column) for column in columns
        )
        table_ref = self._quote_identifier(table_name)
        return await self.query(
            f"SELECT {projection} FROM {table_ref} LIMIT %s", (limit,)
        )

    async def get_foreign_keys(self) -> list[TableRelationship]:
        """Discover foreign key relationships."""
        if self.db_type == "mysql":
            fks = await self.query(
                "SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, "
                "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL",
                (self.database,),
            )
            relationships = []
            for fk in fks:
                relationships.append(TableRelationship(
                    source_table=fk["TABLE_NAME"],
                    source_column=fk["COLUMN_NAME"],
                    target_table=fk["REFERENCED_TABLE_NAME"],
                    target_column=fk["REFERENCED_COLUMN_NAME"],
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    verified=False,
                    source="inferred",
                ))
            return relationships
        if self.db_type in ("postgresql", "kingbase"):
            fks = await self.query(
                "SELECT kcu.table_name, kcu.column_name, "
                "ccu.table_name AS foreign_table, ccu.column_name AS foreign_column "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = tc.constraint_name "
                " AND ccu.table_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "AND kcu.table_schema = current_schema()",
            )
            relationships = []
            for fk in fks:
                relationships.append(TableRelationship(
                    source_table=fk["table_name"],
                    source_column=fk["column_name"],
                    target_table=fk["foreign_table"],
                    target_column=fk["foreign_column"],
                    relationship_type=RelationshipType.MANY_TO_ONE,
                    verified=False,
                    source="inferred",
                ))
            return relationships
        return []

    # ── Deep exploration ────────────────────────────────────

    async def count_distinct(self, table_name: str, column: str) -> int:
        """Get approximate distinct count for a column."""
        val = await self.query_value(
            f"SELECT COUNT(DISTINCT `{column}`) as cnt FROM `{table_name}`", ()
        )
        return val if val else 0

    async def null_ratio(self, table_name: str, column: str) -> float:
        """Get NULL ratio for a column."""
        total = await self.query_value(
            f"SELECT COUNT(*) as cnt FROM `{table_name}`",
        )
        if not total:
            return 0.0
        nulls = await self.query_value(
            f"SELECT COUNT(*) as cnt FROM `{table_name}` WHERE `{column}` IS NULL",
        )
        return (nulls / total) if total > 0 else 0.0

    async def verify_join_cardinality(
        self,
        source_table: str,
        source_column: str,
        target_table: str,
        target_column: str,
    ) -> float:
        """Verify the cardinality of a join. Returns ratio: #source rows / #matched target rows."""
        sql = (
            f"SELECT COUNT(DISTINCT s.`{source_column}`) as src_cnt, "
            f"COUNT(DISTINCT t.`{target_column}`) as tgt_cnt "
            f"FROM `{source_table}` s "
            f"LEFT JOIN `{target_table}` t ON s.`{source_column}` = t.`{target_column}`"
        )
        row = await self.query_one(sql)
        if row and row["tgt_cnt"] and row["tgt_cnt"] > 0:
            return row["src_cnt"] / row["tgt_cnt"]
        return 0.0

    async def value_distribution(self, table_name: str, column: str, top_n: int = 10) -> list[dict]:
        """Get value distribution for a column."""
        return await self.query(
            f"SELECT `{column}` as value, COUNT(*) as cnt "
            f"FROM `{table_name}` "
            f"GROUP BY `{column}` "
            f"ORDER BY cnt DESC LIMIT %s",
            (top_n,),
        )

    # ── Light exploration (one-shot) ────────────────────────

    async def explore_schema_light(self, source_db_id: str) -> SchemaExploration:
        """Run full light exploration: tables, columns, samples, FKs."""
        exploration = SchemaExploration(source_db_id=source_db_id, phase="light")

        tables = await self.list_tables()
        logger.info(f"Exploring {len(tables)} tables in {self.database}")

        try:
            for table_name in tables:
                try:
                    table_schema = await self.get_table_info(table_name)
                    safe_columns = safe_sample_columns(table_schema)
                    sample_rows = await self.sample_data(
                        table_name, limit=10, columns=safe_columns,
                    )
                    apply_safe_samples(table_schema, sample_rows, max_values=5)
                    exploration.tables.append(table_schema)
                except Exception as e:
                    logger.warning(f"Failed to explore table {table_name}: {e}")

            # Foreign keys
            try:
                fks = await self.get_foreign_keys()
                exploration.relationships = fks
            except Exception as e:
                logger.warning(f"Failed to get FKs: {e}")
        finally:
            await self.aclose()

        return exploration


def take_schema_snapshot(table_schema: TableSchema, source_db_id: str) -> SchemaSnapshot:
    """Take a snapshot of the current schema for drift detection."""
    return SchemaSnapshot(
        table_name=table_schema.name,
        source_db_id=source_db_id,
        fields={f.name: f.data_type for f in table_schema.fields},
        primary_keys=table_schema.primary_keys,
    )
