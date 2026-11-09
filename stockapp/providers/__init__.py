"""Data providers (INDmoney, Kite, Trendlyne), each reached through an MCP server."""

from __future__ import annotations

from ..config import Settings
from ..mcp_bridge import EventLoopThread, FileTokenStorage, McpConnection
from .base import (
    RANGES,
    InvalidRequest,
    LoginRequired,
    NotConfigured,
    NotFound,
    PlanLimit,
    Provider,
    ProviderError,
    Unavailable,
    Unsupported,
    history_csv,
    normalize_symbol,
)
from .indmoney import INDmoneyProvider
from .kite import KiteProvider
from .trendlyne import TrendlyneProvider

PROVIDER_CLASSES: tuple[type[Provider], ...] = (INDmoneyProvider, KiteProvider, TrendlyneProvider)

__all__ = [
    "RANGES",
    "INDmoneyProvider",
    "InvalidRequest",
    "KiteProvider",
    "LoginRequired",
    "NotConfigured",
    "NotFound",
    "PlanLimit",
    "Provider",
    "ProviderError",
    "TrendlyneProvider",
    "Unavailable",
    "Unsupported",
    "build_providers",
    "history_csv",
    "normalize_symbol",
]


def build_providers(settings: Settings, loop_thread: EventLoopThread | None = None) -> dict[str, Provider]:
    """One provider object per data source; sources without a URL are 'not configured'."""
    loop_thread = loop_thread or EventLoopThread()
    providers: dict[str, Provider] = {}
    for cls in PROVIDER_CLASSES:
        url = settings.provider_urls.get(cls.name)
        connection = None
        if url:
            # OAuth is used only if the server asks for it (HTTP 401).
            storage = FileTokenStorage(
                settings.data_dir / "tokens" / f"{cls.name}.json",
                redirect_uri=f"{settings.public_base_url}/oauth/{cls.name}/callback",
            )
            connection = McpConnection(cls.name, url, loop_thread, storage=storage)
        providers[cls.name] = cls(connection)
    return providers
