from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from requests.exceptions import ReadTimeout

from dylathleen.api.abstract.model import PackageStatus
from dylathleen.api.http import HttpClient
from dylathleen.gems.flatpak.model import FlatpakApplication
from dylathleen.gems.flatpak.worker import FlatpakAsyncDataLoader


class FlatpakAsyncDataLoaderTest(TestCase):

    def test_format_category(self):
        self.assertEqual('irc client', FlatpakAsyncDataLoader.format_category('IRCClient'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('TextEditor'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('Text Editor'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('text editor'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('Text editor'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('text Editor'))
        self.assertEqual('text editor', FlatpakAsyncDataLoader.format_category('textEditor'))
        self.assertEqual('ide', FlatpakAsyncDataLoader.format_category('IDE'))
        self.assertEqual('faster irc client', FlatpakAsyncDataLoader.format_category('Faster IRCClient'))
        self.assertEqual('3d graphics', FlatpakAsyncDataLoader.format_category('3DGraphics'))
        self.assertEqual('32d graphics', FlatpakAsyncDataLoader.format_category('32DGraphics'))
        self.assertEqual('d32 graphics', FlatpakAsyncDataLoader.format_category('D32Graphics'))

    def test_metadata_timeout_keeps_installed_app_ready(self):
        logger = Mock()
        client = HttpClient(logger, sleep=0)
        app = FlatpakApplication(id='org.example.App', installed=True, description='Local description')
        manager = Mock()
        worker = FlatpakAsyncDataLoader(app, manager, SimpleNamespace(http_client=client, logger=logger),
                                        Mock(), Mock())
        with patch.object(client.session, 'get', side_effect=ReadTimeout) as request, redirect_stderr(StringIO()) as stderr:
            worker.run()

        self.assertEqual(1, request.call_count)
        self.assertEqual('', stderr.getvalue())
        logger.warning.assert_called_once()
        self.assertEqual(PackageStatus.READY, app.status)
        self.assertEqual('Local description', app.description)
        manager.cache_to_disk.assert_not_called()
