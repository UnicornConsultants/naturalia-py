import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from product_media import ProductMedia
from engine import Catalog,ROOT

class ProductMediaTests(unittest.TestCase):
 def test_all_catalog_images_resolve_to_public_archive(self):
  catalog=Catalog();self.assertEqual(len(catalog.items),188)
  for product in catalog.items:
   media=catalog.media.get(product['id']);self.assertIsNotNone(media)
   path,mime=media;self.assertIn((ROOT/'data/naturalia-group/assets').resolve(),path.parents)
   self.assertIn(mime,['image/jpeg','image/png']);self.assertLessEqual(path.stat().st_size,5*1024*1024)
   self.assertEqual(product['image'],'/product-images/'+product['id'])
  self.assertIsNone(catalog.media.get('../../.env'))
  self.assertIsNone(catalog.media.get('https://example.com/image.jpg'))
