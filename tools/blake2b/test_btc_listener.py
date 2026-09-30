import unittest

from live_btc_node import peer_options


class ListenerTests(unittest.TestCase):
    def test_offline_default(self):
        self.assertEqual(peer_options(None, None), ['--offline'])

    def test_explicit_lan(self):
        options = peer_options('192.168.8.10', None)
        self.assertIn('--bind-addr=192.168.8.10:19735', options)
        self.assertIn('--autolisten=false', options)
        self.assertIn('--announce-addr-discovered=false', options)
        self.assertIn('--autoconnect-seeker-peers=0', options)
        self.assertNotIn('--offline', options)
        self.assertIn('--bind-addr=10.0.0.2:19736', peer_options('10.0.0.2', 19736))

    def test_invalid_bindings(self):
        for host, port in [(None, 19735), ('0.0.0.0', 19735), ('8.8.8.8', 19735),
                           ('127.0.0.1', 19735), ('224.0.0.1', 19735),
                           ('::', 19735), ('node.local', 19735),
                           ('192.168.8.10', 0), ('192.168.8.10', 65536)]:
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                peer_options(host, port)


if __name__ == '__main__':
    unittest.main()
