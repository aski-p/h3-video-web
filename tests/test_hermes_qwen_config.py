import importlib.util
from pathlib import Path
import tempfile
import unittest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'configure-hermes-qwen38.py'
spec = importlib.util.spec_from_file_location('configure_hermes_qwen38', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class HermesQwenConfigTests(unittest.TestCase):
    def test_replaces_old_route_and_clears_stale_context_cache(self):
        with tempfile.TemporaryDirectory() as value:
            home = Path(value)
            config = home / 'config.yaml'
            config.write_text(yaml.safe_dump({'model': {'provider': 'custom', 'default': 'PassingByPixels/Qwen3.8-27B-NVFP4', 'base_url': 'http://127.0.0.1:8003/v1', 'context_length': 32768}, 'unrelated': {'keep': True}}))
            (home / 'context_length_cache.yaml').write_text('old: 32768\n')
            backup, cleared = module.configure(home)
            result = yaml.safe_load(config.read_text())
            self.assertTrue(backup.is_file())
            self.assertEqual(cleared, [home / 'context_length_cache.yaml'])
            self.assertFalse((home / 'context_length_cache.yaml').exists())
            self.assertEqual(result['model']['provider'], 'custom')
            self.assertEqual(result['model']['default'], module.MODEL)
            self.assertEqual(result['model']['base_url'], module.BASE_URL)
            self.assertEqual(result['model']['context_length'], module.CONTEXT_LENGTH)
            self.assertTrue(result['unrelated']['keep'])


if __name__ == '__main__':
    unittest.main()
