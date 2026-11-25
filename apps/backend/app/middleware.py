import asyncio
import re

from starlette.responses import JSONResponse


class BoundedBodyMiddleware:
    """Bound buffers and concurrent imports before multipart or JSON parsing."""

    def __init__(self, app, max_bytes: int = 131072):
        self.app = app
        self.max_bytes = max_bytes
        self.large_slots = asyncio.Semaphore(2)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        project_path = r"/api/v1/projects/[0-9a-fA-F-]{36}/"
        path = scope.get("path", "")
        upload = scope["method"] == "POST" and re.fullmatch(
            project_path + r"(?:imports/docx|assets/reference-images|assets/music)", path
        )
        plan = scope["method"] in {"POST", "PUT"} and re.fullmatch(
            project_path + r"(?:inputs|storyboard)", path
        )
        limit = 5 * 1024**2 + 65536 if upload else 1024**2 if plan else self.max_bytes
        if upload or plan:
            if self.large_slots.locked():
                return await JSONResponse(
                    {"detail": "Import capacity is busy; retry shortly"}, 429
                )(scope, receive, send)
            await self.large_slots.acquire()
            try:
                return await self._bounded_call(scope, receive, send, limit)
            finally:
                self.large_slots.release()
        return await self._bounded_call(scope, receive, send, limit)

    async def _bounded_call(self, scope, receive, send, limit):
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length:
            try:
                too_large = int(length) > limit or int(length) < 0
            except ValueError:
                too_large = True
            if too_large:
                return await JSONResponse({"detail": "Request body too large"}, 413)(
                    scope, receive, send
                )
        chunks, size = [], 0
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body = message.get("body", b"")
                    size += len(body)
                    if size > limit:
                        return await JSONResponse({"detail": "Request body too large"}, 413)(
                            scope, receive, send
                        )
                    chunks.append(body)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timed out"}, 408)(
                scope, receive, send
            )
        delivered = False

        async def limited_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, limited_receive, send)
