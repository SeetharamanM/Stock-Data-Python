"""Settings, read from environment variables (and a .env file, see app.py)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URLS = {
    "indmoney": "https://mcp.indmoney.com/mcp",
    "kite": "https://mcp.kite.trade/mcp",
    "trendlyne": "",  # personal URL from your Trendlyne MCP subscription
}


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    data_dir: Path = Path("instance")
    provider_urls: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_URLS))
    allowed_hosts: frozenset[str] = frozenset()
    debug: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        host = env.get("HOST", "127.0.0.1")
        port = int(env.get("PORT", "8000"))
        public_base_url = env.get("PUBLIC_BASE_URL", f"http://localhost:{port}").rstrip("/")
        urls = {
            name: env.get(f"{name.upper()}_MCP_URL", default).strip()
            for name, default in DEFAULT_URLS.items()
        }
        # Requests must name this server, which blocks DNS-rebinding attacks on a local app.
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
        public = urlsplit(public_base_url)
        if public.netloc:
            allowed.add(public.netloc)
        allowed.update(h.strip() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip())
        return cls(
            host=host,
            port=port,
            public_base_url=public_base_url,
            data_dir=Path(env.get("DATA_DIR", "instance")),
            provider_urls=urls,
            allowed_hosts=frozenset(allowed),
            debug=env.get("FLASK_DEBUG", "") in ("1", "true", "True"),
        )
