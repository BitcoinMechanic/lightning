import json
from pathlib import Path
import sys
import tempfile
import unittest

from quote_api_service import install, NAME


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = self.root/'settings.json'
        self.settings.write_text(json.dumps(dict(deployment='operator-pair-v1',
                                               python=sys.executable, receiver_id='02'+'11'*32)))
        self.settings.chmod(0o600)
        self.units = self.root/'units'

    def test_default_repeat_preserves_token_and_unit(self):
        result = install(self.settings, self.units)
        token = (self.root/'customer-api.json').read_bytes()
        unit = (self.units/NAME).read_bytes()
        self.assertFalse(result['auto_process_new_quotes'])
        self.assertFalse(result['services_started'])
        install(self.settings, self.units)
        self.assertEqual(token, (self.root/'customer-api.json').read_bytes())
        self.assertEqual(unit, (self.units/NAME).read_bytes())
        self.assertNotIn(b'--auto-process', unit)
        self.assertNotIn(json.loads(token)['token'].encode(), unit)
        self.assertEqual((self.root/'customer-api.json').stat().st_mode & 0o777, 0o600)

    def test_explicit_automatic_mode(self):
        self.assertTrue(install(self.settings, self.units, auto_process=True)['auto_process_new_quotes'])
        unit = (self.units/NAME).read_text()
        self.assertIn('"--auto-process"', unit)
        self.assertIn('WantedBy=cln-swaps.target', unit)
        self.assertNotIn('receiver.service', unit)

    def test_mode_change_refused(self):
        install(self.settings, self.units)
        with self.assertRaises(ValueError):
            install(self.settings, self.units, auto_process=True)

    def test_foreign_unit_and_symlink_refused_before_credentials(self):
        self.units.mkdir()
        dest = self.units/NAME
        dest.write_text('unrelated')
        with self.assertRaises(ValueError):
            install(self.settings, self.units)
        dest.unlink()
        dest.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):
            install(self.settings, self.units)
        self.assertFalse((self.root/'customer-api.json').exists())

    def test_invalid_port_or_deployment_no_credentials(self):
        for port in (0, 65536, True):
            with self.assertRaises(ValueError):
                install(self.settings, self.units, port=port)
        self.settings.write_text(json.dumps(dict(deployment='old')))
        with self.assertRaises(ValueError):
            install(self.settings, self.units)
        self.assertFalse((self.root/'customer-api.json').exists())

    def test_wrong_existing_token_binding_preserved(self):
        path = self.root/'customer-api.json'
        path.write_text(json.dumps(dict(token='aa'*32, payer_id='wrong')))
        path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            install(self.settings, self.units)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse((self.units/NAME).exists())


if __name__ == '__main__':
    unittest.main()
