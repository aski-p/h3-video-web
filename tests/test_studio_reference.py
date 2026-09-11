import hashlib
import io
import json
import unittest
from studio_reference import transfer, validate_request, NoRedirect

class Reply(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}

class TransferTests(unittest.TestCase):
    def setUp(self):
        self.video = b'video-data' * 100
        self.sha = hashlib.sha256(self.video).hexdigest()
        self.data = dict(url='https://hyovtguangyykehxwnvp.supabase.co/storage/v1/object/sign/aski-cast-transfer/00000000-0000-4000-8000-000000000000.mp4?token=private',
                         sha256=self.sha, size=len(self.video), name='aski-'+self.sha+'.mp4')
    def test_destination_and_file_integrity(self):
        calls=[]
        class Client:
            def open(_, req, timeout):
                calls.append(req)
                if isinstance(req,str): return Reply(self.video)
                self.assertEqual(req.full_url,'https://h3.example/api/refv/set')
                self.assertEqual(req.data,self.video)
                self.assertEqual(req.headers['X-origin'],'secret')
                return Reply(json.dumps({'ok':True,'refv':{'size':len(self.video),'sha256':self.sha}}).encode())
        result=transfer(json.dumps(self.data).encode(),backend='https://h3.example',origin_header='X-Origin',origin_secret='secret',client_header='X-Client',client_key='key',opener=Client())
        self.assertEqual(result['sha256'],self.sha)
        self.assertEqual(len(calls),2)
    def test_ssrf_and_nonvideo_inputs_rejected(self):
        for url in ['http://127.0.0.1/private','https://evil.example/a?token=x',self.data['url'].replace('aski-cast-transfer','other-bucket'),self.data['url'].replace('.mp4?','.html?'),self.data['url'].split('?')[0]]:
            with self.assertRaises(ValueError):validate_request(json.dumps({**self.data,'url':url}).encode())
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,None,None,None,'https://evil.invalid')
    def test_mismatched_bytes_never_reach_nas(self):
        class Client:
            def open(_, req, timeout):
                self.assertIsInstance(req,str)
                return Reply(b'not-the-upload')
        with self.assertRaises(ValueError):transfer(json.dumps(self.data).encode(),backend='https://h3.example',origin_header='X-Origin',origin_secret='secret',client_header='X-Client',client_key='key',opener=Client())

if __name__=='__main__':unittest.main()
