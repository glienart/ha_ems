"""
HA REST API client.

Inside a HA add-on, the Supervisor injects:
  - SUPERVISOR_TOKEN  : long-lived token for the HA API
  - The HA API is reachable at http://supervisor/core/api
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

_LOGGER = logging.getLogger(__name__)

HA_API = "http://supervisor/core/api"


def _headers() -> dict:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def get_state(entity_id: str) -> Optional[dict]:
    """Return the state dict for an entity, or None if unavailable."""
    url = f"{HA_API}/states/{entity_id}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=_headers())
            if r.status_code == 200:
                return r.json()
            _LOGGER.warning("get_state %s -> %s", entity_id, r.status_code)
    except Exception as exc:
        _LOGGER.error("get_state %s error: %s", entity_id, exc)
    return None


async def get_float(entity_id: str, default: float = 0.0) -> float:
    """Return the numeric state of an entity."""
    if not entity_id:
        return default
    data = await get_state(entity_id)
    if data is None:
        return default
    try:
        return float(data["state"])
    except (KeyError, ValueError, TypeError):
        return default


async def get_bool(entity_id: str) -> bool:
    """Return True if entity state is 'on'."""
    if not entity_id:
        return False
    data = await get_state(entity_id)
    return data is not None and data.get("state") == "on"


async def call_service(domain: str, service: str, data: dict) -> bool:
    """Call a HA service. Returns True on success."""
    url = f"{HA_API}/services/{domain}/{service}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, headers=_headers(), json=data)
            return r.status_code in (200, 201)
    except Exception as exc:
        _LOGGER.error("call_service %s.%s error: %s", domain, service, exc)
        return False


async def set_entity_state(entity_id: str, state: str, attributes: dict = None) -> bool:
    """
    Create or update a virtual sensor state in HA.
    These show up as regular entities and can be used in Lovelace.
    """
    url = f"{HA_API}/states/{entity_id}"
    payload: dict[str, Any] = {"state": state}
    if attributes:
        payload["attributes"] = attributes
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, headers=_headers(), json=payload)
            return r.status_code in (200, 201)
    except Exception as exc:
        _LOGGER.error("set_entity_state %s error: %s", entity_id, exc)
        return False


async def turn_on(entity_id: str) -> bool:
    return await call_service("homeassistant", "turn_on", {"entity_id": entity_id})


async def turn_off(entity_id: str) -> bool:
    return await call_service("homeassistant", "turn_off", {"entity_id": entity_id})
