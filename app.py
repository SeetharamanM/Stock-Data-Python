"""Start the Stock Data web app.

    python app.py            # then open http://localhost:8000

Settings come from environment variables or a .env file (see .env.example).
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()

from stockapp import create_app  # noqa: E402  (after load_dotenv so settings see .env)
from stockapp.config import Settings  # noqa: E402

settings = Settings.from_env()
app = create_app(settings)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx2", "mcp"):  # per-request logs from the MCP client
        logging.getLogger(noisy).setLevel(logging.WARNING)
    configured = [name for name, url in settings.provider_urls.items() if url]
    print(f"Stock Data: {settings.public_base_url}  (data sources: {', '.join(configured) or 'none'})")
    # The reloader would restart the process on code changes and drop provider logins.
    app.run(host=settings.host, port=settings.port, debug=settings.debug, use_reloader=False, threaded=True)
