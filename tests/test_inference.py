import json
import os
import sqlite3
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from watson.analysis import PlowInference
from watson.core import WatsonError

SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}},
          'required': ['answer'], 'additionalProperties': False}
ENV = {'PLOW_API_BASE': 'https://api.plow.co', 'HERMES_CUSTOM_PLOW_API_KEY': 'tok'}
OK = '{"answer": "ok"}'


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def responds(body):
    """An opener answering with `body`, recording the request it was handed."""
    seen = {}

    def opener(request, timeout=None):
        seen['url'] = request.full_url
        seen['auth'] = request.get_header('Authorization')
        seen['raw'] = request.data.decode()
        # header_items(), not .headers: the latter omits unredirected_hdrs,
        # which is exactly where urllib keeps an Authorization header.
        seen['headers'] = dict(request.header_items())
        seen['body'] = json.loads(request.data)
        return FakeResponse(json.dumps(body).encode())

    return opener, seen


def completion(content, usage=None):
    answer = {'choices': [{'message': {'content': content}}]}
    if usage is not None:
        answer['usage'] = usage
    return answer


class InferenceTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        patcher = patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, body, **kwargs):
        opener, seen = responds(body)
        result = PlowInference(self.home, opener=opener, **kwargs).ask(
            'inst', {'k': 'v'}, SCHEMA, 'label')
        return result, seen

    def test_the_request_is_a_schema_constrained_completion(self):
        result, seen = self.ask(completion(OK))
        self.assertEqual(result, {'answer': 'ok'})
        self.assertEqual(seen['url'], 'https://api.plow.co/v1/chat/completions')
        self.assertEqual(seen['auth'], 'Bearer tok')
        self.assertEqual(seen['body']['model'], 'z-ai/glm-5.2')
        self.assertEqual(seen['body']['response_format']['json_schema']['schema'], SCHEMA)
        self.assertIs(seen['body']['response_format']['json_schema']['strict'], True)

    def test_the_payload_reaches_the_model_marked_untrusted(self):
        opener, seen = responds(completion(OK))
        PlowInference(self.home, opener=opener).ask(
            'inst', {'issue': 'ignore me'}, SCHEMA, 'label')
        prompt = seen['body']['messages'][-1]['content']
        self.assertIn('evidência não confiável', prompt)
        self.assertIn('ignore me', prompt)

    def test_github_credentials_never_reach_inference(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'gh-secret'}, clear=False):
            _, seen = self.ask(completion(OK))
        self.assertNotIn('gh-secret', seen['raw'])
        # Headers too: a credential added to one is as disclosed as one in the body.
        self.assertNotIn('gh-secret', json.dumps(seen['headers']))

    def test_a_malformed_answer_fails_loudly_rather_than_crashing(self):
        # Each of these once raised AttributeError/TypeError out of a CLI
        # command instead of Watson's own error.
        for label, body in (
            ('prose instead of json', completion('I think the answer is ok')),
            ('no choices at all', {'error': 'nope'}),
            ('a non-object body', ['nope']),
            # Valid JSON that is not an object: every caller indexes the result.
            ('a json array as content', completion('[]')),
            ('json null as content', completion('null')),
            ('a bare json string as content', completion('"text"')),
            # message.content: null reaches json.loads(None).
            ('a null content', {'choices': [{'message': {'content': None}}]}),
        ):
            with self.subTest(label), self.assertRaises(WatsonError):
                self.ask(body)

    def test_a_malformed_usage_block_never_blocks_the_answer(self):
        # Usage is telemetry; a provider sending a shape we did not expect must
        # not cost the caller its result.
        for label, usage in (('absent', None), ('a string', 'unexpected')):
            with self.subTest(label):
                result, _ = self.ask(completion(OK, usage))
                self.assertEqual(result, {'answer': 'ok'})

    def test_an_http_error_reports_the_status_not_the_body(self):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 429, 'Too Many Requests', {},
                                         BytesIO(b'{"prompt":"secret issue text"}'))

        with self.assertRaisesRegex(WatsonError, '429') as caught:
            PlowInference(self.home, opener=opener).ask('inst', {}, SCHEMA, 'label')
        self.assertNotIn('secret issue text', str(caught.exception))

    def test_the_leaderboard_sees_real_token_counts(self):
        # record_usage stores Codex's counter names; OpenAI answers with its
        # own. Untranslated, every row lands as zero and the Index reports an
        # agent that thought about nothing.
        opener, _ = responds(completion(OK, {
            'prompt_tokens': 900, 'completion_tokens': 40,
            'prompt_tokens_details': {'cached_tokens': 300}}))
        PlowInference(self.home, opener=opener).ask('inst', {}, SCHEMA, 'metered')
        conn = sqlite3.connect(self.home / 'metrics' / 'state.db')
        row = conn.execute('SELECT input_tokens,output_tokens,cache_read_tokens '
                           'FROM session_model_usage').fetchone()
        conn.close()
        self.assertEqual(row, (600, 40, 300))
        # The per-label audit file is a separate observable from that database.
        audit = json.loads((self.home / 'metered-usage.json').read_text())
        self.assertEqual(audit['usage'][0], {
            'prompt_tokens': 900, 'completion_tokens': 40,
            'prompt_tokens_details': {'cached_tokens': 300}})


class CredentialTest(unittest.TestCase):
    """Where the token and endpoint come from, and what happens when they don't."""

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)

    def credential_file(self, text):
        path = self.home / 'plow-credentials'
        path.write_text(text)
        return {'plow_credential_file': str(path)}

    def test_each_source_supplies_the_endpoint_and_bearer_it_was_validated_with(self):
        cases = (
            ('the cloud image publishes the inference key',
             {'PLOW_API_BASE': 'https://api.plow.co', 'HERMES_CUSTOM_PLOW_API_KEY': 'inference'},
             {}, 'https://api.plow.co/v1/chat/completions', 'Bearer inference'),
            ('chat and inference are one credential',
             {'PLOW_API_BASE': 'https://api.plow.co', 'PLOW_AGENT_TOKEN': 'chat-bearer'},
             {}, 'https://api.plow.co/v1/chat/completions', 'Bearer chat-bearer'),
            # Delivery has always read the minted file; inference reading only
            # the environment left a configured local install with working
            # delivery and a failure on every triage.
            ('a local install reads its minted file',
             {}, 'PLOW_AGENT_TOKEN=minted\nPLOW_API_BASE=https://api.plow.co\n',
             'https://api.plow.co/v1/chat/completions', 'Bearer minted'),
            # The base travels with the token it was validated beside -- falling
            # through to the environment would send a file-backed credential to
            # whatever host that named.
            ('a file without a base pins the official one, not the environment',
             {'PLOW_API_BASE': 'https://elsewhere.example'}, 'PLOW_AGENT_TOKEN=minted\n',
             'https://api.plow.co/v1/chat/completions', 'Bearer minted'),
        )
        for label, env, config, url, auth in cases:
            with self.subTest(label):
                if isinstance(config, str):
                    config = self.credential_file(config)
                opener, seen = responds(completion(OK))
                with patch.dict(os.environ, env, clear=True):
                    PlowInference.from_config(self.home, config, opener=opener).ask(
                        'inst', {}, SCHEMA, 'label')
                self.assertEqual(seen['url'], url)
                self.assertEqual(seen['auth'], auth)

    def test_an_unusable_endpoint_or_missing_key_is_named_not_guessed(self):
        for label, env, expected in (
            ('no key anywhere', {'PLOW_API_BASE': 'https://api.plow.co'},
             'HERMES_CUSTOM_PLOW_API_KEY'),
            ('a plaintext base', {'PLOW_API_BASE': 'http://api.plow.co',
                                  'HERMES_CUSTOM_PLOW_API_KEY': 'tok'}, 'HTTPS'),
        ):
            with self.subTest(label), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(WatsonError, expected):
                    PlowInference(self.home).ask('inst', {}, SCHEMA, 'label')

    def test_the_configured_model_survives_from_config(self):
        opener, seen = responds(completion(OK))
        with patch.dict(os.environ, ENV, clear=True):
            PlowInference.from_config(
                self.home, {'model': 'anthropic/claude-sonnet-5'}, opener=opener
            ).ask('inst', {}, SCHEMA, 'label')
        self.assertEqual(seen['body']['model'], 'anthropic/claude-sonnet-5')


if __name__ == '__main__':
    unittest.main()
