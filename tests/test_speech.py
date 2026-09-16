import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from watson.core import WatsonError
from watson.speech import read_aloud, generate_voice
from watson.delivery import deliver


class Response(io.BytesIO):
    def __init__(self, data=b'ID3-test', mime='audio/mpeg'):
        super().__init__(data)
        self.headers = {'Content-Type': mime}


class SpeechTests(unittest.TestCase):
    def test_native_voice_never_falls_back_to_attachment(self):
        from unittest.mock import Mock
        store, plow = Mock(), Mock()
        with self.assertRaisesRegex(WatsonError, 'voicememo'):
            deliver(store, 1, plow, audio='sample.mp3')
        self.assertEqual(store.mock_calls, [])
        self.assertEqual(plow.mock_calls, [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'voice.mp3'

    def test_sol_subscription_contract_and_private_output(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, 'https://chatgpt.com/backend-api/pronunciation/synthesize?format=mp3&voice=sol')
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
                self.assertEqual(request.get_header('Chatgpt-account-id'), 'account')
                self.assertEqual(request.get_header('Originator'), 'codex_ios')
                body = json.loads(request.data)
                self.assertEqual(body, {'pronunciation_audio_text': 'Olá!', 'pronunciation_language': 'pt', 'speed': 1, 'text': 'Olá!'})
                return Response()
        read_aloud(' Olá! ', self.path, credentials=lambda: ('test-token', 'account'), opener=Opener())
        self.assertEqual(self.path.read_bytes(), b'ID3-test')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(WatsonError):
            read_aloud('Olá!', self.path)

    def test_provider_error_does_not_leak_or_fall_back(self):
        class Opener:
            def open(inner, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 401, 'private-secret', {}, io.BytesIO(b'private-secret'))
        with self.assertRaises(WatsonError) as caught:
            read_aloud('Olá!', self.path, credentials=lambda: ('token', None), opener=Opener())
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('private-secret', str(caught.exception))
        self.assertFalse(self.path.exists())
        with patch('watson.speech.read_aloud', side_effect=WatsonError('Unavailable')), patch('watson.speech.local_voice') as local:
            with self.assertRaises(WatsonError): generate_voice('Oi', self.path, {})
            local.assert_not_called()

    def test_non_audio_response_is_not_written(self):
        class Opener:
            def open(inner, request, timeout):
                return Response(b'<html>Error</html>')
        with self.assertRaises(WatsonError):
            read_aloud('Olá!', self.path, credentials=lambda: ('token', None), opener=Opener())
        self.assertFalse(self.path.exists())

    def test_invalid_input_rejected_before_credentials(self):
        def forbidden(): self.fail('Credentials accessed for invalid input')
        for text, voice, path in [('', 'sol', self.path), ('x' * 131073, 'sol', self.path),
                                  ('Oi', 'invalid', self.path), ('Oi', 'sol', self.path.with_suffix('.m4a'))]:
            with self.assertRaises(WatsonError):
                read_aloud(text, path, voice, credentials=forbidden)
