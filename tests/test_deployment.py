from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent


class DeploymentTests(unittest.TestCase):
    def test_database_and_model_have_no_public_ports(self):
        services = yaml.safe_load((ROOT / 'compose.yaml').read_text())['services']
        for name in ['db', 'ollama', 'worker', 'webhook', 'polling']:
            self.assertNotIn('ports', services[name])
        self.assertEqual(services['ollama']['environment']['OLLAMA_NO_CLOUD'], '1')
        self.assertEqual(services['worker']['depends_on']['model-init']['condition'], 'service_completed_successfully')
        self.assertEqual(services['polling']['profiles'], ['polling'])
        self.assertEqual(services['webhook']['profiles'], ['production'])
