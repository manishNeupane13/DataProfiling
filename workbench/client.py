import requests

class API:
    def __init__(self, base='http://127.0.0.1:8000'):
        self.base=base.rstrip('/')
    def call(self, method, path, **kwargs):
        response=requests.request(method,self.base+path,timeout=120,**kwargs)
        response.raise_for_status()
        return response
    def get(self,path): return self.call('GET',path).json()
    def post(self,path,body): return self.call('POST',path,json=body).json()
