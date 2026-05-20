from __future__ import annotations

import uvicorn

from main import app


def main() -> None:
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=18081,
        log_level="info",
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()