"""Shared security boundaries for roles and application logs."""
import logging
import os
import re


# Self-service roles must not grant moderation, administrative or mass-ping powers.
PRIVILEGED_PERMISSIONS = (
    'administrator', 'manage_guild', 'manage_roles', 'manage_channels',
    'manage_messages', 'manage_threads', 'manage_webhooks', 'manage_events',
    'manage_nicknames', 'pin_messages', 'bypass_slowmode',
    'manage_emojis_and_stickers', 'manage_expressions', 'kick_members',
    'ban_members', 'moderate_members', 'view_audit_log', 'view_guild_insights',
    'view_creator_monetization_analytics',
    'mention_everyone', 'mute_members', 'deafen_members', 'move_members',
)


def privileged_role(role):
    return any(getattr(role.permissions, name, False) for name in PRIVILEGED_PERMISSIONS)


def redact(value):
    text = str(value)
    # Include configured secrets even if a library logs them without field names.
    for name, secret in os.environ.items():
        if re.search(r'(TOKEN|PASSWORD|SECRET|API_KEY)$', name) and secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(signature=)[^&\s\'"<>]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(/webhooks/\d+/)[^/?\s\'"<>]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(/interactions/\d+/)[^/?\s\'"<>]+', r'\1[REDACTED]', text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        # Redact after formatting so tracebacks and library exceptions are covered.
        return redact(super().format(record))
