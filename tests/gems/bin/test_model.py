from unittest import TestCase

from dylathleen.gems.bin.model import BinApplication


class BinPublisherTest(TestCase):

    def test_repository_owner_or_provider(self):
        for source, provider, expected in (
                ('https://github.com/marcosnils/bin/releases/tag/v0.29.4', 'github', 'marcosnils'),
                ('codeberg.org/mergiraf/mergiraf', 'codeberg', 'mergiraf'),
                ('https://gitlab.com/Owner/tool/-/releases/v1', 'gitlab', 'Owner'),
                ('https://github.com/', 'github', 'github'),
                ('https://releases.example.org/v1/tool', 'httprelease', 'releases.example.org'),
                ('docker://busybox:latest', 'docker', 'docker'),
                ('https://[', 'github', 'github')):
            with self.subTest(source=source):
                entry = {'url': source, 'provider': provider, 'version': 'v1'}
                package = BinApplication('/usr/local/bin/tool', entry, True, {'bin.scope.system': 'System'})
                self.assertEqual(package.get_publisher(), expected)
