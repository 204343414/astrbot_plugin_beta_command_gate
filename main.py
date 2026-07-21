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
        """Return the client for the event's platform; fail closed if ambiguous."""
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            meta = getattr(event, "platform_meta", None)
            platform_id = str(getattr(meta, "id", "") or "")

        matches = []
        try:
            for platform in self.context.platform_manager.get_insts():
                meta = getattr(platform, "meta", None)
                candidate_id = str(
                    getattr(meta, "id", None)
                    or getattr(platform, "platform_id", None)
                    or getattr(platform, "id", None)
                    or ""
                )
                if platform_id and candidate_id != platform_id:
                    continue
                get_client = getattr(platform, "get_client", None)
                client = get_client() if callable(get_client) else None
                if client and (hasattr(client, "get_group_member_info") or hasattr(client, "call_action")):
                    matches.append(client)
        except Exception as exc:
            logger.warning("[BetaCommandGate] failed to discover OneBot client: %s", exc)
            return None

        # With a platform id there should be exactly one. Without one, only
        # accept a single client; choosing an arbitrary bot is unsafe.
        return matches[0] if len(matches) == 1 else None

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
        """OneBot get_group_member_info: any successful member result grants access."""
        try:
            if hasattr(client, "get_group_member_info"):
                result = await client.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
            else:
                result = await client.call_action("get_group_member_info", group_id=int(group_id), user_id=int(user_id))
            return bool(result)
        except (TypeError, ValueError):
            logger.warning("[BetaCommandGate] non-numeric OneBot group/user id configured: group=%r user=%r", group_id, user_id)
        except Exception as exc:
            # Includes member-not-found, bot removed from beta group, and API
            # failures. All are intentionally fail-closed.
            logger.info("[BetaCommandGate] membership lookup denied group=%s user=%s: %s", group_id, user_id, exc)
        return False

    async def _deny(self, event: AstrMessageEvent, reason: str) -> None:
        logger.info("[BetaCommandGate] command blocked: %s", reason)
        if str(self.config.get("deny_mode", "silent")) == "message":
            await event.send(event.plain_result(str(self.config.get("deny_message", "该指令仅向内测群成员开放。"))))
        event.stop_event()
