import os
from typing import Any, Dict

import httpx


class PicApiClient:
    def __init__(self):
        self.timeout = httpx.Timeout(connect=10.0, read=1200.0, write=1200.0, pool=10.0)
        self.base_url = os.getenv("PICAPI_URL", "http://picapi:8000").rstrip("/")

    def abs_url(self, u: str) -> str:
        if not u:
            return u
        if u.startswith("http://") or u.startswith("https://"):
            return u
        if not u.startswith("/"):
            u = "/" + u
        return f"{self.base_url}{u}"

    async def get(self, endpoint: str, **params) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        payload = {k: v for k, v in params.items() if v is not None and v != ""}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(url, params=payload)
            r.raise_for_status()
            return r.json()

    async def post(self, endpoint: str, payload: Any) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
