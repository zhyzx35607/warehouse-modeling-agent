"""Secret Manager + SM4 fallback credential provider.

Credential resolution order:
  1. External Secret Manager (Vault / AWS / GCP) — if configured and reachable
  2. SM4-encrypted password in PostgreSQL source_db_registry — fallback
  3. Environment variables — last resort

Usage:
    provider = get_credential_provider()
    creds = await provider.get_credentials("ecommerce_mysql")
    # → {host, port, user, password, database}

The agent NEVER stores plaintext passwords in its state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from wren_agent.config import settings

logger = logging.getLogger(__name__)


# ── Backend implementations ──────────────────────────────────────


class VaultBackend:
    """HashiCorp Vault backend (KV v2)."""

    def __init__(self, url: str, token: str, mount_point: str = "secret"):
        import httpx
        self._url = url.rstrip("/")
        self._token = token
        self._mount = mount_point
        self._client = httpx.AsyncClient(
            headers={"X-Vault-Token": token},
            timeout=10,
        )

    async def get(self, path: str, key: str = "password") -> str:
        """Read a secret from Vault KV v2.

        Path: secret/<path> → GET {url}/v1/{mount}/data/{path}
        """
        try:
            resp = await self._client.get(
                f"{self._url}/v1/{self._mount}/data/{path}"
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("data", {})
                return data.get(key, "")
            logger.warning(f"Vault get '{path}': HTTP {resp.status_code}")
            return ""
        except Exception as e:
            logger.warning(f"Vault unreachable: {e}")
            raise

    async def close(self):
        await self._client.aclose()


class AWSBackend:
    """AWS Secrets Manager backend."""

    def __init__(self, region: str):
        self._region = region

    async def get(self, path: str, key: str = "password") -> str:
        """Read a secret from AWS Secrets Manager."""
        try:
            import json
            loop = asyncio.get_running_loop()

            def _get():
                import boto3
                client = boto3.client("secretsmanager", region_name=self._region)
                resp = client.get_secret_value(SecretId=path)
                if "SecretString" in resp:
                    secret = json.loads(resp["SecretString"])
                    return secret.get(key, "")
                return ""

            return await loop.run_in_executor(None, _get)
        except Exception as e:
            logger.warning(f"AWS Secrets Manager unreachable: {e}")
            raise


class GCPBackend:
    """Google Cloud Secret Manager backend."""

    def __init__(self, project: str):
        self._project = project

    async def get(self, path: str, key: str = "password") -> str:
        """Read a secret from GCP Secret Manager."""
        try:
            loop = asyncio.get_running_loop()

            def _get():
                from google.cloud import secretmanager
                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{self._project}/secrets/{path}/versions/latest"
                resp = client.access_secret_version(request={"name": name})
                return resp.payload.data.decode("UTF-8")

            return await loop.run_in_executor(None, _get)
        except Exception as e:
            logger.warning(f"GCP Secret Manager unreachable: {e}")
            raise


class EnvBackend:
    """Environment variable backend — for development only."""

    async def get(self, path: str, key: str = "password") -> str:
        import os
        env_key = f"DB_PASSWORD_{path.upper().replace('/', '_').replace('-', '_')}"
        return os.getenv(env_key, "")


# ── Backend factory ──────────────────────────────────────────────


def _create_backend():
    """Create the backend based on config. Returns None if no backend configured."""
    backend = settings.secret.backend

    if backend == "vault":
        if settings.secret.vault_url and settings.secret.vault_token:
            return VaultBackend(
                url=settings.secret.vault_url,
                token=settings.secret.vault_token,
                mount_point=settings.secret.vault_mount_point,
            )

    elif backend == "aws_secrets":
        if settings.secret.aws_region:
            return AWSBackend(region=settings.secret.aws_region)

    elif backend == "gcp_secret":
        if settings.secret.gcp_project:
            return GCPBackend(project=settings.secret.gcp_project)

    elif backend == "env":
        return EnvBackend()

    # backend == "none" or unconfigured → return None
    return None


# ── Credential Provider (unified + fallback) ─────────────────────


class CredentialProvider:
    """Unified credential resolution with Secret Manager → SM4 fallback.

    Resolution order:
      1. Secret Manager (Vault/AWS/GCP/Env) — if configured
      2. SM4-encrypted password in PostgreSQL — always available fallback
      3. Environment variables — last resort for doris/ds configs
    """

    def __init__(self):
        self._backend = _create_backend()
        self._backend_name = settings.secret.backend

        if self._backend:
            logger.info(f"Secret Manager backend: {self._backend_name}")
        else:
            logger.info("No Secret Manager configured — using SM4-encrypted PG passwords")

    async def _try_secret_manager(self, path: str, key: str = "password") -> str:
        """Try to get password from Secret Manager. Returns empty on failure."""
        if not self._backend:
            return ""

        try:
            return await self._backend.get(path, key)
        except Exception:
            return ""

    async def _try_sm4_fallback(self, db_id_or_name: str) -> dict | None:
        """Try to get credentials from PG (SM4-encrypted password)."""
        try:
            from wren_agent.tools.pg_store import get_source_db_credentials
            return await get_source_db_credentials(db_id_or_name)
        except Exception as e:
            logger.debug(f"SM4 fallback unavailable for '{db_id_or_name}': {e}")
            return None

    async def get_credentials(self, db_id_or_name: str) -> dict:
        """Get full DB connection credentials.

        Returns {host, port, user, password, database, db_type}.
        Raises ValueError if no credentials found.
        """
        # Step 1: Try Secret Manager
        secret_path = f"database/{db_id_or_name}"
        sm_password = await self._try_secret_manager(secret_path)

        if sm_password:
            # Got password from SM — now get connection info from PG registry
            try:
                from wren_agent.tools.pg_store import get_source_db
                row = await get_source_db(db_id_or_name)
                if row:
                    return {
                        "host": row["host"],
                        "port": row["port"],
                        "user": row["user_name"],
                        "password": sm_password,
                        "database": row["database_name"],
                        "db_type": row["db_type"],
                        "source": "secret_manager",
                    }
            except Exception:
                pass

        # Step 2: Fall back to SM4-encrypted password in PG
        creds = await self._try_sm4_fallback(db_id_or_name)
        if creds and creds.get("password"):
            creds["source"] = "sm4_pg_fallback"
            return creds

        # Step 3: Fail
        raise ValueError(
            f"No credentials found for '{db_id_or_name}'. "
            f"Secret Manager ({self._backend_name}) and SM4 fallback both failed. "
            f"Register the database via POST /api/v1/source-dbs first."
        )

    async def get_doris_credentials(self) -> dict:
        """Get Doris connection credentials from config (not Secret Manager)."""
        return {
            "host": settings.doris.host,
            "port": settings.doris.query_port,
            "user": settings.doris.user,
            "password": settings.doris.password,
            "database": settings.doris.database,
            "source": "config",
        }

    async def get_ds_credentials(self) -> dict:
        """Get Dolphin Scheduler connection info."""
        return {
            "base_url": settings.ds.base_url,
            "api_key": settings.ds.api_key,
            "source": "config",
        }

    async def health_check(self) -> dict:
        """Check Secret Manager availability."""
        result = {
            "backend": self._backend_name,
            "available": self._backend is not None,
        }

        if self._backend:
            try:
                # Quick connectivity test
                import time
                t0 = time.monotonic()
                await self._backend.get("__health_check__", "test")
                result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                result["reachable"] = True
            except Exception as e:
                result["reachable"] = False
                result["error"] = str(e)[:200]
                result["fallback_available"] = True  # SM4 always available

        # /health aggregation keys off "status" — without it this service
        # rendered with an empty status and was excluded from the count.
        if self._backend is None:
            result["status"] = "unconfigured"
        elif result.get("reachable"):
            result["status"] = "healthy"
        else:
            result["status"] = "degraded"  # backend down, SM4 fallback works

        return result


# ── Singleton ───────────────────────────────────────────────────

_provider: CredentialProvider | None = None


def get_credential_provider() -> CredentialProvider:
    """Get or create the credential provider singleton."""
    global _provider
    if _provider is None:
        _provider = CredentialProvider()
    return _provider
