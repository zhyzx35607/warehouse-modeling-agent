"""Centralized configuration for the Wren Modeling Agent.

All settings loaded from environment variables with typed defaults.
Uses pydantic-settings for validation and .env file support.

Priority: env var > .env file > default

Usage:
    from wren_agent.config import settings
    ds_url = settings.ds_base_url
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ so nested BaseSettings models pick it up
_ENV_FILE = Path(__file__).parent.parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and value and key not in os.environ:
                    os.environ[key] = value


class DolphinSchedulerSettings(BaseSettings):
    """Dolphin Scheduler connection settings."""

    model_config = SettingsConfigDict(env_prefix="DS_")

    base_url: str = Field(
        default="http://127.0.0.1:12345/dolphinscheduler",
        description="Dolphin Scheduler API base URL",
    )
    api_key: str = Field(
        default="",
        description="DS API authentication token",
    )
    request_timeout: int = Field(default=60, description="HTTP request timeout in seconds")
    poll_interval: int = Field(default=10, description="Poll interval for task status (seconds)")
    max_poll_cycles: int = Field(default=60, description="Max poll cycles before timeout")
    default_failure_strategy: str = Field(default="CONTINUE")
    default_warning_type: str = Field(default="NONE")

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)


class PostgresSettings(BaseSettings):
    """PostgreSQL connection settings."""

    model_config = SettingsConfigDict(env_prefix="PG_")

    dsn: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/wren",
        description="PostgreSQL connection DSN",
    )
    pool_min_size: int = Field(default=5, ge=1, le=50)
    pool_max_size: int = Field(default=20, ge=1, le=100)
    pool_timeout: int = Field(default=30, description="Connection pool timeout (seconds)")

    @property
    def is_configured(self) -> bool:
        return bool(self.dsn)

    @property
    def safe_dsn(self) -> str:
        """Return the configured DSN unchanged; TLS policy belongs to deployment."""
        return self.dsn


class Neo4jSettings(BaseSettings):
    """Neo4j graph database settings."""

    model_config = SettingsConfigDict(env_prefix="NEO4J_")

    uri: str = Field(default="bolt://localhost:7687")
    user: str = Field(default="neo4j")
    password: str = Field(default="")
    database: str = Field(default="neo4j")
    max_connection_lifetime: int = Field(default=3600)
    max_connection_pool_size: int = Field(default=50)

    @property
    def is_configured(self) -> bool:
        return bool(self.uri and self.user)


class DorisSettings(BaseSettings):
    """Doris data warehouse connection settings."""

    model_config = SettingsConfigDict(env_prefix="DORIS_")

    host: str = Field(default="localhost")
    fe_http_port: int = Field(default=8030, description="FE HTTP port")
    query_port: int = Field(default=9030, description="MySQL protocol query port")
    user: str = Field(default="root")
    password: str = Field(default="")
    database: str = Field(default="wren_dw")
    replication_num: int = Field(
        default=1,
        description="Replication number for CREATE TABLE DDLs (set to number of BE nodes)",
    )
    request_timeout: int = Field(default=300, description="Query timeout (seconds)")
    table_name_length_limit: int = Field(
        default=64,
        description=(
            "Doris FE table_name_length_limit (max table-name length). MUST "
            "match the cluster: the runtime value is changeable via "
            "`ADMIN SET FRONTEND CONFIG (\"table_name_length_limit\" = \"256\")` "
            "and must be persisted in fe.conf to survive FE restarts. The "
            "harness shortens over-long plan table names and validates against "
            "this value."
        ),
    )

    @property
    def is_configured(self) -> bool:
        return bool(self.host)


class LLMSettings(BaseSettings):
    """LLM (Language Model) settings for LangChain integration."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: Literal["openai", "deepseek", "ollama", "custom"] = Field(
        default="deepseek",
        description="LLM provider",
    )
    model: str = Field(
        default="deepseek-chat",
        description="Model name",
    )
    api_key: str = Field(default="", description="API key")
    api_base: str = Field(
        default="https://api.deepseek.com/v1",
        description="API base URL (for OpenAI-compatible endpoints)",
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(
        default=16384,
        ge=1,
        description="Default max tokens per LLM response (overridden per call kind)",
    )
    # qwen-plus (production model via DashScope compatible mode) caps output
    # at 32768 tokens — any higher max_tokens is rejected with HTTP 400 and
    # EVERY design call silently falls back to programmatic generation
    # (observed in real-task acceptance: 590 programmatic mirror tables,
    # zero DWS/ADS). Defaults sit at the cap; `tokens_for()` clamps to
    # `max_tokens_ceiling` so a stray env override can never re-break it.
    max_tokens_design: int = Field(
        default=32768, ge=1, description="Max tokens for layer design JSON generation",
    )
    max_tokens_review: int = Field(
        default=4096, ge=1, description="Max tokens for optional post-design LLM review",
    )
    max_tokens_grill: int = Field(
        default=4096, ge=1, description="Max tokens for grill clarifications",
    )
    max_tokens_report: int = Field(
        default=8192, ge=1, description="Max tokens for report / narrative chat",
    )
    max_tokens_semantic: int = Field(
        default=4096, ge=1, description="Max tokens for batched semantic assertions",
    )
    max_tokens_repair: int = Field(
        default=32768, ge=1, description="Max tokens for design_revise repair",
    )
    max_tokens_classify: int = Field(
        default=12800, ge=1, description="Max tokens for error classification LLM call",
    )
    max_tokens_ceiling: int = Field(
        default=32768, ge=1,
        description="Hard ceiling for per-call max_tokens (provider output "
                    "limit — qwen-plus rejects anything above 32768 with "
                    "HTTP 400, silently degrading every design call).",
    )
    layer_review: bool = Field(
        default=False,
        description="When true, run an extra LLM review after each design layer "
                    "(adds ~5 serial LLM calls; off by default for latency)",
    )
    request_timeout: int = Field(default=300, description="LLM request timeout (seconds)")
    max_retries: int = Field(default=2)
    json_object_mode: bool = Field(
        default=True,
        description="Ask OpenAI-compatible endpoints for response_format="
                    "{'type': 'json_object'} on generate_json calls. Endpoints "
                    "that reject the format fall back to plain invocation "
                    "automatically; set false to disable outright.",
    )
    compiler_enabled: bool = Field(
        default=True,
        description="ADR-0015: DWS/ADS design uses the semantic-design "
                    "contract — the LLM outputs `semantic_designs` (columns "
                    "with source_expr, sources, filters) and the "
                    "DesignCompiler renders DDL/load SQL deterministically. "
                    "Legacy (LLM-written SQL) remains available by setting "
                    "false.",
    )
    allow_programmatic_fallback: bool = Field(
        default=False,
        description="Explicit legacy ODS/DIM/DWD fallback opt-in; live model failures raise by default",
    )
    embedding_api_base: str = Field(default="", description="Separate OpenAI-compatible embedding endpoint; empty disables semantic enrichment")
    embedding_api_key: str = Field(default="", description="Embedding provider credential")
    embedding_model: str = Field(
        default="text-embedding-v3",
        description="Embedding model for field-semantics vector retrieval "
                    "(DashScope OpenAI-compatible /embeddings endpoint, 1024-dim)",
    )

    def tokens_for(self, kind: str) -> int:
        """Resolve per-call max_tokens budget by kind (clamped to the
        provider's output ceiling — see ``max_tokens_ceiling``)."""
        budgets = {
            "design": self.max_tokens_design,
            "review": self.max_tokens_review,
            "grill": self.max_tokens_grill,
            "report": self.max_tokens_report,
            "semantic": self.max_tokens_semantic,
            "repair": self.max_tokens_repair,
            "classify": self.max_tokens_classify,
        }
        return min(budgets.get(kind, self.max_tokens), self.max_tokens_ceiling)

    @property
    def is_configured(self) -> bool:
        if self.provider == "ollama":
            return bool(self.model)
        return bool(self.api_key)


class SM4Settings(BaseSettings):
    """SM4 encryption settings for stored passwords."""

    model_config = SettingsConfigDict(env_prefix="SM4_")

    key: str = Field(
        default="",
        description="SM4 encryption key (16 bytes, will be padded/truncated to 16)",
    )


class SecretManagerSettings(BaseSettings):
    """Secret Manager backend settings."""

    model_config = SettingsConfigDict(env_prefix="SECRET_")

    backend: Literal["vault", "aws_secrets", "gcp_secret", "env", "none"] = Field(
        default="env",
        description="Secret manager backend type",
    )
    # Vault
    vault_url: str = Field(default="http://localhost:8200")
    vault_token: str = Field(default="")
    vault_mount_point: str = Field(default="secret")
    # AWS
    aws_region: str = Field(default="us-east-1")
    # GCP
    gcp_project: str = Field(default="")


class AgentSettings(BaseSettings):
    """Agent behavior settings."""

    model_config = SettingsConfigDict(env_prefix="AGENT_")

    max_retries: int = Field(default=3, ge=0, le=20, description="Max execution retries")
    retry_backoff_base: int = Field(default=30, description="Base seconds for exponential backoff")
    deep_explore_max_queries_per_table: int = Field(default=5)
    explore_concurrency: int = Field(
        default=6,
        ge=1,
        le=32,
        description="Deprecated — light explore uses batched catalog queries",
    )
    explore_max_tables: int = Field(
        default=200,
        ge=1,
        le=5000,
        description="Max tables explored per source database (light explore)",
    )
    execute_concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Max concurrent ODS _sync workflow starts in execute_tasks",
    )
    sensitive_field_confidence_threshold: float = Field(default=0.7)
    grill_max_questions_per_round: int = Field(default=10)
    semantic_validation_tolerance: float = Field(default=0.05, description="±5% tolerance for semantic assertions")
    blueprint_match_threshold: float = Field(default=0.3, description="Minimum similarity to use blueprint")
    task_timeout_hours: int = Field(default=72, description="Max task lifetime before auto-cancel")
    audit_retention_days: int = Field(default=90)


class ServerSettings(BaseSettings):
    """API server settings."""

    model_config = SettingsConfigDict(env_prefix="SERVER_")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    reload: bool = Field(default=False, description="Auto-reload on code changes (dev only)")

    @property
    def is_dev(self) -> bool:
        return self.reload


# ── Root settings ───────────────────────────────────────────────


class Settings(BaseSettings):
    """Aggregated settings for the entire Wren Modeling Agent."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Use case_sensitive=False so DS_BASE_URL and ds_base_url both work
        case_sensitive=False,
    )

    ds: DolphinSchedulerSettings = Field(default_factory=DolphinSchedulerSettings)
    pg: PostgresSettings = Field(default_factory=PostgresSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    doris: DorisSettings = Field(default_factory=DorisSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    sm4: SM4Settings = Field(default_factory=SM4Settings)
    secret: SecretManagerSettings = Field(default_factory=SecretManagerSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)

    # Convenience order for the project root
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).parent.parent.resolve(),
        exclude=True,
    )


# ── Singleton ───────────────────────────────────────────────────

settings = Settings()


# ── Helper: generate .env template ──────────────────────────────


def generate_env_template() -> str:
    """Generate a commented .env template string."""
    return """# ============================================================
# Wren Modeling Agent Configuration
# Copy to .env and fill in values
# ============================================================

# ── Dolphin Scheduler ──────────────────────────────────────────
DS_BASE_URL=http://127.0.0.1:12345/dolphinscheduler
DS_API_KEY=your-ds-api-key
# DS_REQUEST_TIMEOUT=60
# DS_POLL_INTERVAL=10
# DS_MAX_POLL_CYCLES=60

# ── PostgreSQL ─────────────────────────────────────────────────
PG_DSN=postgresql://postgres:postgres@localhost:5432/wren
# PG_POOL_MIN_SIZE=5
# PG_POOL_MAX_SIZE=20

# ── Neo4j ──────────────────────────────────────────────────────
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password-here
# NEO4J_DATABASE=neo4j

# ── Doris ──────────────────────────────────────────────────────
DORIS_HOST=localhost
# DORIS_FE_HTTP_PORT=8030
# DORIS_QUERY_PORT=9030
DORIS_USER=root
DORIS_PASSWORD=your-password-here
DORIS_DATABASE=wren_dw

# ── LLM ────────────────────────────────────────────────────────
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=your-deepseek-api-key
LLM_API_BASE=https://api.deepseek.com/v1
# LLM_TEMPERATURE=0.0
# LLM_MAX_TOKENS=8192
# LLM_MAX_TOKENS_DESIGN=8192
# LLM_MAX_TOKENS_REVIEW=1024
# LLM_MAX_TOKENS_GRILL=2048
# LLM_MAX_TOKENS_REPORT=2048
# LLM_MAX_TOKENS_SEMANTIC=2048
# LLM_MAX_TOKENS_REPAIR=8192
# LLM_MAX_TOKENS_CLASSIFY=12800
# LLM_LAYER_REVIEW=false
# LLM_REQUEST_TIMEOUT=300
# LLM_MAX_RETRIES=2
# AGENT_EXPLORE_CONCURRENCY=6

# ── Secret Manager ─────────────────────────────────────────────
# Options: vault | aws_secrets | gcp_secret | env | none
SECRET_BACKEND=env

# ── Agent Behavior ─────────────────────────────────────────────
# AGENT_MAX_RETRIES=3
# AGENT_RETRY_BACKOFF_BASE=30
# AGENT_TASK_TIMEOUT_HOURS=72

# ── Server ─────────────────────────────────────────────────────
# SERVER_HOST=0.0.0.0
# SERVER_PORT=8000
# SERVER_LOG_LEVEL=INFO
"""


if __name__ == "__main__":
    # For testing: print current config (mask sensitive values)
    import json

    def safe_repr(obj) -> dict:
        d = {}
        for k, v in obj.__dict__.items():
            if isinstance(v, BaseSettings):
                d[k] = safe_repr(v)
            elif "key" in k.lower() or "password" in k.lower() or "secret" in k.lower() or "token" in k.lower():
                d[k] = "***" if v else "(not set)"
            else:
                d[k] = v
        return d

    print("=== Current Configuration ===")
    print(json.dumps(safe_repr(settings), indent=2, ensure_ascii=False, default=str))
    print()
    print("=== .env Template ===")
    print(generate_env_template())
