import hashlib
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from dylathleen.gems.debget import generator

FIXTURES = Path(__file__).with_name('fixtures')


class Response:
    def __init__(self, payload=None, body=b'', url='https://example.invalid/test'):
        self.payload = payload
        self.body = body
        self.url = url

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        return [self.body]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class FixtureAPI:
    def __init__(self, name):
        self.payload = json.loads((FIXTURES / name).read_text())
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload if url.endswith('/releases') else {'description': 'Fixture application'})


def evaluate(recipe, forge, assets, codename, arch):
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'cache'
        if forge.github:
            path.write_text('\n'.join(f'    "browser_download_url": "{a.url}",' for a in assets))
        else:
            path.write_text('\n'.join(a.url for a in assets))
        recipe_file = Path(directory) / 'generated'
        recipe_file.write_text(recipe)
        code = '''
        function get_github_releases() { :; }
        function get_gitlab_releases() { :; }
        source "$1"
        printf '%s\\n%s\\n' "$URL" "$VERSION_PUBLISHED"
        '''
        result = subprocess.check_output(['/bin/bash', '-c', code, 'bash', str(recipe_file)], text=True,
                                         env={'PATH': '/usr/bin:/bin', 'ACTION': 'show', 'CACHE_FILE': str(path),
                                              'UPSTREAM_CODENAME': codename, 'HOST_ARCH': arch})
        return tuple(result.splitlines())


class GeneratorTest(TestCase):
    def test_turbojedi_selects_exact_os_and_arch_and_dynamic_version(self):
        forge = generator.Forge('https://github.com/cyanide-burnout/TurboJedi', session=FixtureAPI('turbojedi-releases.json'))
        _, releases = forge.analyse()
        self.assertEqual([r[0].tag for r in releases], ['v0.17', 'v0.16'])
        rules = []
        for a in releases[0]:
            codename, arch = generator.guessed_platform(a)
            distro, number = generator.PLATFORM[codename]
            actual = generator.Metadata('turbojedi', f'0.17~{distro}{number}', arch, 'Turbo Vision')
            rule = generator.infer_rule(a, actual, codename, arch, releases[1:])
            generator.check_rule(rule, a, actual, releases)
            rules.append(rule)
        recipe = generator.recipe(forge, 'turbojedi', 'TurboJedi', 'Turbo Vision viewer', rules)
        for codename, (distro, number) in generator.PLATFORM.items():
            for arch in ('amd64', 'arm64'):
                url, version = evaluate(recipe, forge, releases[1], codename, arch)
                self.assertTrue(url.endswith(f'turbojedi_0.16_{distro}-{number}_{arch}.deb'))
                self.assertEqual(version, f'0.16~{distro}{number}')

    def test_dylathleen_all_and_shell_quoting(self):
        forge = generator.Forge('https://github.com/konkere/dylathleen', 'sample-token',
                                FixtureAPI('dylathleen-releases.json'))
        _, releases = forge.analyse()
        a = releases[0][0]
        info = generator.Metadata('dylathleen', '0.4.0-1', 'all', '')
        rule = generator.infer_rule(a, info, '', 'all', releases[1:])
        recipe = generator.recipe(forge, 'dylathleen', "Dylath-Leen", "Name's $(bad-command)", [rule])
        self.assertEqual(evaluate(recipe, forge, releases[1], 'jammy', 'arm64')[1], '0.3.3-1')
        self.assertEqual(evaluate(recipe, forge, releases[0] * 2, 'jammy', 'arm64'), ('', ''))
        self.assertIn("Authorization", forge.session.calls[0][1]['headers'])
        self.assertNotIn("sample-token", recipe)
        with self.assertRaises(generator.DebGetError):
            generator.check_rule(rule, a, info, [releases[0] * 2])

    def test_gitlab_nested_repository_and_asset_link(self):
        api = FixtureAPI('gitlab-releases.json')
        forge = generator.Forge('https://gitlab.com/vendor/team/tool', 'not-for-gitlab', api)
        _, releases = forge.analyse()
        self.assertTrue(api.calls[1][0].endswith('vendor%2Fteam%2Ftool/releases'))
        self.assertNotIn('Authorization', api.calls[0][1]['headers'])
        asset = releases[0][0]
        info = generator.Metadata('tool', '1.4.2', 'arm64', 'Tool')
        rule = generator.infer_rule(asset, info, '', 'arm64', releases[1:])
        recipe = generator.recipe(forge, 'tool', 'Tool', 'Tool description', [rule])
        self.assertIn('get_gitlab_releases vendor/team/tool latest', recipe)
        self.assertEqual(evaluate(recipe, forge, releases[1], 'noble', 'arm64')[1], '1.4.1')
        self.assertEqual(evaluate(recipe, forge, releases[1], 'noble', 'amd64'), ('', ''))

    def test_download_checks_deb_metadata_and_sha256(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'DEBIAN').mkdir()
            (root / 'DEBIAN/control').write_text('Package: example\nVersion: 1.2.3-5\nArchitecture: all\n'
                                                 'Maintainer: Fixture <test@example.invalid>\nDescription: Fixture\n')
            artifact = root / 'example.deb'
            subprocess.run(['dpkg-deb', '--build', str(root), str(artifact)], check=True, stdout=subprocess.DEVNULL)
            body = artifact.read_bytes()
            class Download:
                def get(self, url, **kwargs):
                    return Response(body=body, url=url)
            url = 'https://example.invalid/example.deb'
            digest = hashlib.sha256(body).hexdigest()
            a = generator.Asset('example.deb', url, 'v1.2.3', 'sha256:' + digest)
            self.assertEqual(generator.inspect_asset(a, Download()).version, '1.2.3-5')
            with self.assertRaisesRegex(generator.DebGetError, 'checksum'):
                generator.inspect_asset(generator.Asset(a.name, url, a.tag, 'sha256:wrong'), Download())

    def test_read_sha256_sidecar_when_api_has_no_digest(self):
        class Download:
            def get(self, url, **kwargs):
                return Response(body=(b'a' * 64) + b' *example.deb\n', url=url)

        forge = generator.Forge('https://github.com/owner/example', session=Download())
        asset = generator.Asset('example.deb', 'https://example.invalid/example.deb', 'v1')
        assets = forge._digests([asset], {'SHA256SUMS': 'https://example.invalid/SHA256SUMS'})
        self.assertEqual(assets[0].digest, 'sha256:' + 'a' * 64)

    def test_generator_refuses_untrusted_urls(self):
        for url in ('https://github.com/owner/../tool', 'http://github.com/owner/tool',
                    'https://github.com@evil.invalid/owner/tool', 'https://gitlab.com/owner/main?exec=true'):
            with self.assertRaises(generator.DebGetError):
                generator.Forge(url)
