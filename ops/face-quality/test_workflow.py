import hashlib
import tempfile
import unittest
from pathlib import Path

from workflow import original_from_manifest

class OriginalManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root/'original.mp4'
        self.video.write_bytes(b'verified video fixture')
        self.manifest = {'decodeVerified':True,'assets':[{'decodeVerified':True,'path':self.video.name,'sha256':hashlib.sha256(self.video.read_bytes()).hexdigest()}]}

    def test_verified_original_is_accepted(self):
        self.assertEqual(original_from_manifest(self.root,self.manifest), self.video)

    def test_changed_original_is_rejected(self):
        self.video.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'integrity'):
            original_from_manifest(self.root,self.manifest)

    def test_path_outside_nas_is_rejected(self):
        self.manifest['assets'][0]['path'] = '../outside.mp4'
        with self.assertRaisesRegex(ValueError,'escapes'):
            original_from_manifest(self.root,self.manifest)

    def test_unverified_asset_is_rejected(self):
        self.manifest['assets'][0]['decodeVerified'] = False
        with self.assertRaisesRegex(ValueError,'integrity'):
            original_from_manifest(self.root,self.manifest)

if __name__ == '__main__':
    unittest.main()
