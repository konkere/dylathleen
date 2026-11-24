from unittest import TestCase

from dylathleen.gems.debget.catalogue import Recipe, fields
from dylathleen.gems.debget.model import DebGetApplication


class DebGetPublisherTest(TestCase):
    def test_owner_takes_precedence_over_platform(self):
        cases = (
            ('WEBSITE=https://github.com/konkere/dylathleen', 'konkere'),
            ('WEBSITE=https://example.org\nget_github_releases "cyanide-burnout/TurboJedi" latest', 'cyanide-burnout'),
            ('get_gitlab_releases "ProjectOwner%2Fgroup%2Fproject" latest', 'ProjectOwner'),
            ('URL=https://codeberg.org/mergiraf/mergiraf/releases/download/v1/test.deb', 'mergiraf'),
            ('WEBSITE=https://github.com/', 'github'),
            ('WEBSITE=https://vendor.example.org', 'vendor.example.org'),
            ('APT_REPO_URL="https://vendor.example.org stable main"', 'vendor.example.org'),
            ('get_github_releases "${OWNER}/${PROJECT}" latest', 'github'),
            ('WEBSITE=https://[\nURL=https://download.example.org/test.deb', 'download.example.org'),
        )
        for recipe_source, expected in cases:
            with self.subTest(recipe=recipe_source):
                recipe = Recipe('test-app', '01-main', recipe_source, fields(recipe_source))
                app = DebGetApplication('test-app', recipe, None, None, None, {})
                self.assertEqual(app.get_publisher(), expected)
