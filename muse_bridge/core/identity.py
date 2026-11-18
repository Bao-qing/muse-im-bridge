"""Trusted identity keys; opaque platform IDs are never numerically normalized."""
from dataclasses import dataclass


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and value != "*" and not any(c.isspace() for c in value))


@dataclass(frozen=True)
class AdapterIdentity:
    platform: str
    account: str


@dataclass(frozen=True)
class ChannelKey:
    identity: AdapterIdentity
    kind: str
    target: str


@dataclass(frozen=True)
class ActorKey:
    channel: ChannelKey
    user: str
