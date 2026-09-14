"""Transport guard that runs for HTTP *and* WebSocket scopes.

Starlette's BaseHTTPMiddleware (``@app.middleware('http')``) never sees a
``websocket`` scope, so host allowlisting and rate limiting implemented there
silently exempt the gateway. This is plain ASGI middleware precisely so both
transports get the same checks, and so oversized JSON bodies are rejected
before framework parsing, including chunked bodies.
"""
from urllib.parse import urlparse
from starlette.responses import JSONResponse

ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1', 'testserver', 'testclient'}
WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
JSON_BODY_LIMIT = 65536
AUTH_RATE = 30
API_RATE = 600
GATEWAY_RATE = 60
MAX_BUCKETS = 2000


def _header(scope, name):
    name = name.encode()
    for key, value in scope.get('headers', ()):
        if key == name:
            return value.decode('latin-1')
    return None


def _hostname(value):
    if not value:
        return None
    # Strip the port, tolerating bracketed IPv6 literals.
    if value.startswith('['):
        return value.partition(']')[0][1:].lower()
    return value.partition(':')[0].lower()


class LabGuard:
    """Host allowlist, cross-origin write rejection, rate limit, body cap."""

    def __init__(self, app, public_origin='', limit=JSON_BODY_LIMIT):
        self.app, self.limit = app, limit
        self.public_origin = (public_origin or '').rstrip('/')
        self.public_host = _hostname(urlparse(self.public_origin).netloc) if self.public_origin else None
        self.buckets = {}

    # -- checks ---------------------------------------------------------
    def allowed_hosts(self):
        hosts = set(ALLOWED_HOSTS)
        if self.public_host:
            hosts.add(self.public_host)
        return hosts

    def host_ok(self, scope):
        host = _hostname(_header(scope, 'host'))
        # A missing Host header cannot be matched against the allowlist.
        return host is not None and host in self.allowed_hosts()

    def origin_ok(self, scope):
        """Reject cross-origin writes and cross-origin gateway handshakes.

        Safe only because host_ok() has already pinned the Host header to the
        allowlist: deriving the expected origin from an attacker-controlled
        Host is what let DNS rebinding through the old WebSocket check.
        """
        origin = _header(scope, 'origin')
        if not origin:
            # Non-browser clients omit Origin; they still need a token.
            return True
        if self.public_origin:
            return origin.rstrip('/') == self.public_origin
        scheme = {'ws': 'http', 'wss': 'https'}.get(scope.get('scheme', 'http'), scope.get('scheme', 'http'))
        return origin.rstrip('/') == f"{scheme}://{_header(scope, 'host')}"

    def rate_ok(self, scope, path):
        client = scope.get('client')
        ip = client[0] if client else 'unknown'
        if scope['type'] == 'websocket':
            kind, limit = 'gateway', GATEWAY_RATE
        elif path.startswith('/api/auth/'):
            kind, limit = 'auth', AUTH_RATE
        elif path.startswith('/api/'):
            kind, limit = 'api', API_RATE
        else:
            return True
        import time
        window = int(time.time() // 60)
        key = (ip, kind)
        previous, count = self.buckets.get(key, (window, 0))
        count = count + 1 if previous == window else 1
        self.buckets[key] = (window, count)
        if len(self.buckets) > MAX_BUCKETS:
            # Evict only stale windows. A global clear would let any client
            # reset every other client's budget by flooding the map.
            for stale in [k for k, (w, _) in self.buckets.items() if w != window]:
                del self.buckets[stale]
        return count <= limit

    # -- rejection ------------------------------------------------------
    async def reject(self, scope, receive, send, status, code, message, ws_code=1008):
        if scope['type'] == 'websocket':
            # Closing without accepting fails the handshake.
            await send({'type': 'websocket.close', 'code': ws_code})
            return
        response = JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path = scope.get('path', '')

        if not self.host_ok(scope):
            return await self.reject(scope, receive, send, 403, 'host', 'Host not permitted for this lab.')
        if scope['type'] == 'websocket' or scope.get('method') in WRITE_METHODS:
            if not self.origin_ok(scope):
                return await self.reject(scope, receive, send, 403, 'origin', 'Cross-origin writes are not allowed.')
        if not self.rate_ok(scope, path):
            return await self.reject(scope, receive, send, 429, 'rate_limit',
                                     'Too many requests. Please pause before retrying.', ws_code=1013)

        buffered = (scope['type'] == 'http' and scope.get('method') in {'POST', 'PUT', 'PATCH'}
                    and path.startswith('/api/') and not path.endswith('/audio'))
        if not buffered:
            return await self.app(scope, receive, send)

        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > self.limit:
                return await self.reject(scope, receive, send, 413, 'body_size',
                                         'JSON body exceeds the 64 KB limit.')
            if not message.get('more_body', False):
                break
        supplied = False

        async def bounded_receive():
            nonlocal supplied
            if not supplied:
                supplied = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        return await self.app(scope, bounded_receive, send)
