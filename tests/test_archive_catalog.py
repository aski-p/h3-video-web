import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import original_video as v
class ArchiveCatalogTests(unittest.TestCase):
 def test_archive_without_manual_review_is_eligible_but_invalid_file_is_not(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);config=root/'config';config.mkdir();(config/'workflow.json').write_text(json.dumps({'nasRoot':str(root)}))
   folder=root/'instagram/account/cycle/post';folder.mkdir(parents=True);video=folder/'video.mp4';video.write_bytes(b'video')
   asset={'path':str(video.relative_to(root)),'sha256':'a'*64,'bytes':5,'decodeVerified':True,'fps':'24/1','duration':6,'frames':144,'width':720,'height':1280}
   manifest=folder/'manifest.json';manifest.write_text(json.dumps({'sourceUrl':'https://www.instagram.com/test/reel/ABC/','assets':[asset]}))
   with patch.object(v,'CONFIG',config):
    rows=v.catalog();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['selectionBasis'],'registered_archive');self.assertEqual(rows[0]['duration'],6)
    video.write_bytes(b'corrupt');self.assertEqual(v.catalog(),[])
