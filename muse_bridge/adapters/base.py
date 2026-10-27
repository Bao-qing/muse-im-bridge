"""Base adapter interface for multi-platform chat bridges."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the adapter (e.g., 'qq', 'onebot', 'wechat')."""
        pass

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this adapter is enabled in configuration."""
        pass

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter listener and any associated background services."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter and clean up resources."""
        pass
