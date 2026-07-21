"""OneBot beta-member command gate for AstrBot.

This plugin deliberately only decides whether a protected command may propagate.
It does not reserve quotas, invoke commands, or infer whether a downstream plugin
succeeded.  A downstream plugin owns its own business-side effects.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@dataclass(frozen=True)
class _CacheEntry:
    allowed: bool
    expires_at: float


@register(
    "astrbot_plugin_beta_command_gate",
    "Local customization",
    "OneBot 内测成员指令门禁",
    "0.1.0",
)
class BetaCommandGate(Star):
    """Stops configured commands unless sender is in a configured beta group."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._membership_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._membership_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @filter.regex(r"[\s\S]*", priority=100)
    async def gate_protected_commands(self, event: AstrMessageEvent):
        """Run before normal command handlers and stop unauthorized commands."""
        if not bool(self.config.get("enabled", True)):
            return
        if not self._is_protected_command(getattr(event, "message_str", "")):
            return

        # This authorization model is intentionally group-only: a user cannot
        # use a beta command from private chat.
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)
        if not group_id or not user_id:
            await self._deny(event, "protected command was not sent from a group")
            return

        beta_groups = self._beta_group_ids()
        if not beta_groups:
            # Fail closed. An empty eligibility group list must never make a
            # protected feature public due to a configuration mistake.
            await self._deny(event, "no beta_group_ids configured")
            return

        client = self._get_onebot_client(event)
        if client is None:
            await self._deny(event, "matching OneBot client unavailable")
            return

        if not await self._is_beta_member(client, user_id, beta_groups):
            await self._deny(event, "sender is not a current beta-group member")

    def _is_protected_command(self, message: str) -> bool:
        """Match only the first token, optionally preceded by AstrBot's slash."""
        text = str(message or "").strip()
        if not text:
            return False
        first = text.split(maxsplit=1)[0]
        if first.startswith("/"):
            first = first[1:]
        protected = {str(x).strip().lstrip("/") for x in self.config.get("protected_commands", ["群分析", "group_analysis"]) if str(x).strip()}
        return first in protected

    def _beta_group_ids(self) -> list[str]:
        return [str(x).strip() for x in self.config.get("beta_group_ids", []) if str(x).strip()]

    @staticmethod
    def _get_group_id(event: AstrMessageEvent) -> str:
        try:
            value = event.get_group_id()
            return str(value).strip() if value is not None else ""
        except Exception:
            return ""

    @staticmethod
    def _get_user_id(event: AstrMessageEvent) -> str:
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        value = getattr(sender, "user_id", None)
        return str(value).strip() if value is not None else ""

    def _get_onebot_client(self, event: AstrMessageEvent):
        """Get the exact aiocqhttp client which received this event.

        Uses AstrBot's documented Context.get_platform_inst(platform_id) API;
        do not guess a platform by scanning all loaded instances.
        """
        try:
            if event.get_platform_name() != "aiocqhttp":
                return None
            platform = self.context.get_platform_inst(event.get_platform_id())
            get_client = getattr(platform, "get_client", None)
            client = get_client() if callable(get_client) else None
            # Normal AstrBot client has client.api.call_action(...).
            if client and (hasattr(client, "api") or hasattr(client, "call_action")):
                return client
        except Exception as exc:
            logger.warning("[BetaCommandGate] failed to get event OneBot client: %s", exc)
        return None

    async def _is_beta_member(self, client, user_id: str, beta_groups: list[str]) -> bool:
        for beta_group_id in beta_groups:
            key = (beta_group_id, user_id)
            cached = self._membership_cache.get(key)
            if cached and cached.expires_at > time.monotonic():
                if cached.allowed:
                    return True
                continue

            lock = self._membership_locks.setdefault(key, asyncio.Lock())
            async with lock:
                cached = self._membership_cache.get(key)
                if cached and cached.expires_at > time.monotonic():
                    if cached.allowed:
                        return True
                    continue
                allowed = await self._query_group_member(client, beta_group_id, user_id)
                ttl = max(0, int(self.config.get("membership_cache_seconds", 300)))
                if ttl:
                    self._membership_cache[key] = _CacheEntry(allowed, time.monotonic() + ttl)
                if allowed:
                    return True
        return False

    async def _query_group_member(self, client, group_id: str, user_id: str) -> bool:
        """Query OneBot 11 membership through AstrBot's documented client API."""
        try:
            payload = {"group_id": int(group_id), "user_id": int(user_id)}
            if hasattr(client, "get_group_member_info"):
                result = await client.get_group_member_info(**payload)
            elif hasattr(client, "call_action"):
                result = await client.call_action("get_group_member_info", **payload)
            else:
                # This is the normal AstrBot aiocqhttp path.
                api = getattr(client, "api", None)
                call_action = getattr(api, "call_action", None)
                if not callable(call_action):
                    logger.warning("[BetaCommandGate] OneBot client has no call_action API")
                    return False
                result = await call_action("get_group_member_info", **payload)
            return bool(result)
        except (TypeError, ValueError):
            logger.warning("[BetaCommandGate] non-numeric OneBot group/user id configured: group=%r user=%r", group_id, user_id)
        except Exception as exc:
            # Includes member-not-found, Bot removed from beta group, and API
            # failures. All are intentionally fail-closed.
            logger.info("[BetaCommandGate] membership lookup denied group=%s user=%s: %s", group_id, user_id, exc)
        return False

    async def _deny(self, event: AstrMessageEvent, reason: str) -> None:
        logger.info("[BetaCommandGate] command blocked: %s", reason)
        if str(self.config.get("deny_mode", "silent")) == "message":
            await event.send(event.plain_result(str(self.config.get("deny_message", "该指令仅向内测群成员开放。"))))
        event.stop_event()
