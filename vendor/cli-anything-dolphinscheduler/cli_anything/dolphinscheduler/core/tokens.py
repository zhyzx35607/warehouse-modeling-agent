"""Access-token operations for DolphinScheduler.

An *access token* is the credential a CLI or script sends in the ``token``
header for stateless auth. This module lets an already-authenticated session
(cookie- or token-based) mint, list, and revoke tokens — handy for
bootstrapping headless automation from an interactive login.
"""

from __future__ import annotations

from typing import Any, Optional

from .client import DolphinSchedulerClient

_TOKENS_BASE = "/access-tokens"


def create_token(
    client: DolphinSchedulerClient,
    user_id: int,
    expire_time: str,
    *,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Create an access token for a user.

    Args:
        user_id: The numeric id of the user the token authenticates as.
        expire_time: Expiry as ``"yyyy-MM-dd HH:mm:ss"``.
        token: An explicit token string; when omitted the server generates one.

    Returns:
        The created ``AccessToken`` object, whose ``token`` field is the value
        to send in the ``token`` header on later requests.
    """
    return client.post(
        _TOKENS_BASE,
        data={
            "userId": user_id,
            "expireTime": expire_time,
            "token": token,
        },
    )


def generate_token_string(
    client: DolphinSchedulerClient,
    user_id: int,
    expire_time: str,
) -> str:
    """Generate a token string without persisting it (server-side preview)."""
    return client.post(
        f"{_TOKENS_BASE}/generate",
        data={"userId": user_id, "expireTime": expire_time},
    )


def list_tokens(
    client: DolphinSchedulerClient,
    *,
    page_no: int = 1,
    page_size: int = 50,
    search_val: Optional[str] = None,
) -> dict[str, Any]:
    """Return one page of access tokens."""
    return client.get(
        _TOKENS_BASE,
        params={
            "pageNo": page_no,
            "pageSize": page_size,
            "searchVal": search_val,
        },
    )


def delete_token(client: DolphinSchedulerClient, token_id: int) -> Any:
    """Delete an access token by its numeric id."""
    return client.delete(f"{_TOKENS_BASE}/{token_id}")
