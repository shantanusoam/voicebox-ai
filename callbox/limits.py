"""Reject oversized JSON bodies before framework parsing, including chunked bodies."""
from starlette.responses import JSONResponse

class JsonBodyLimit:
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        target=(scope['type']=='http' and scope.get('method') in {'POST','PUT','PATCH'}
                and scope.get('path','').startswith('/api/')
                and not scope.get('path','').endswith('/audio'))
        if not target:
            return await self.app(scope,receive,send)
        body=bytearray()
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            body.extend(message.get('body',b''))
            if len(body)>self.limit:
                response=JSONResponse({'error':{'code':'body_size','message':'JSON body exceeds the 64 KB limit.'}},status_code=413)
                return await response(scope,receive,send)
            if not message.get('more_body',False): break
        supplied=False
        async def bounded_receive():
            nonlocal supplied
            if not supplied:
                supplied=True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()
        return await self.app(scope,bounded_receive,send)
