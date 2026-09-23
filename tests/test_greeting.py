import asyncio
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from watson.greeting import classify, greeting_reply

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / 'image' / 'hooks' / 'watson-greet' / 'handler.py'
MERGE = ROOT / 'image' / 'watson-config-merge.py'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClassifyTests(unittest.TestCase):
    def test_pure_greetings(self):
        cases = {
            'Oi': 'pt', 'oi!': 'pt', 'oii': 'pt', 'Olá': 'pt', 'ola': 'pt',
            'Ol;a': 'pt',  # typo the owner actually sent
            'Oi Watson': 'pt', 'oi, tudo bem?': 'pt', 'Opa, bom dia!': 'pt', 'oi 👋': 'pt',
            'bom dia!': 'pt', 'boa noite watson': 'pt', 'quem é você?': 'pt',
            'hey': 'en', 'Hi Watson': 'en', 'hello!': 'en', 'hey there': 'en',
            'who are you?': 'en',
        }
        for text, lang in cases.items():
            self.assertEqual(classify(text), lang, text)

    def test_requests_acks_and_progress_asks_go_to_the_llm(self):
        for text in (
            '', '   ', 'watson', 'ok', 'valeu', 'beleza', '42', '#42',
            'oi, investiga a 42', 'oi investiga https://github.com/a/b/issues/1',
            'conecta o codex', 'status da issue 12', 'oi\ninvestiga a 42',
            'hi can you look at issue 7', 'oi ' * 30,
            # Answers / progress asks mid-conversation need context.
            'tudo bem', 'tudo certo', 'opa', 'fala', 'e aí?', 'E aí, tudo certo?',
            'status', 'status?', 'o que falta?', 'tá pronto?', "what's missing?",
            'bom dia, alguma novidade?',
        ):
            self.assertIsNone(classify(text), text)

    def test_greeting_reply_is_exact_speak_this(self):
        report = {'speak_this': 'Oi — sou o Watson.\n\n1. **GitHub** — conectado como delltrak.'}
        with patch('watson.greeting.capabilities_report', return_value=report) as caps:
            self.assertEqual(greeting_reply('Olá!'), report['speak_this'])
        caps.assert_called_once()
        self.assertEqual(caps.call_args.kwargs['language'], 'pt')
        with patch('watson.greeting.capabilities_report') as caps:
            self.assertIsNone(greeting_reply('investiga a 42'))
        caps.assert_not_called()

    def test_greeting_reply_refuses_empty_speak_this(self):
        with patch('watson.greeting.capabilities_report', return_value={'speak_this': ' '}):
            with self.assertRaises(RuntimeError):
                greeting_reply('oi')


class _Platform:
    def __init__(self, value):
        self.value = value


def _source(**overrides):
    fields = dict(platform=_Platform('plow_chat'), chat_type='dm', role_authorized=True,
                  is_bot=False, user_id='owner-key')
    fields.update(overrides)
    return SimpleNamespace(**fields)


class GreetingHookTests(unittest.TestCase):
    """handler.py against a fake gateway.run_turn with the real call shape."""

    def setUp(self):
        calls = self.llm_calls = []

        class GatewayTurnMixin:
            async def _handle_message_with_agent(self, event, source, _quick_key, run_generation):
                return await self._run_agent(
                    message=event.text, context_prompt='ctx', history=[], source=source,
                    session_id='sess-1', session_key='k', persist_user_message=event.text,
                )

            async def _run_agent(self, message, context_prompt, history, source, session_id,
                                 **turn_kwargs):
                calls.append(message)
                return {'final_response': 'LLM reply', 'api_calls': 1}

        self.mixin = GatewayTurnMixin
        gateway = types.ModuleType('gateway')
        run_turn = types.ModuleType('gateway.run_turn')
        run_turn.GatewayTurnMixin = GatewayTurnMixin
        gateway.run_turn = run_turn
        modules = patch.dict(sys.modules, {'gateway': gateway, 'gateway.run_turn': run_turn})
        modules.start()
        self.addCleanup(modules.stop)
        self.handler = _load(HANDLER, 'watson_greet_handler_under_test')
        self.speak = 'Oi — sou o Watson.\n\n1. **GitHub** — conectado como delltrak.'
        caps = patch('watson.greeting.capabilities_report', return_value={'speak_this': self.speak})
        caps.start()
        self.addCleanup(caps.stop)

    def _turn(self, text, source=None, recall_text=None, **event_fields):
        fields = dict(text=text, recall_text=recall_text if recall_text is not None else text,
                      media_urls=[], media_types=[], message_type=SimpleNamespace(value='text'))
        fields.update(event_fields)
        runner = self.mixin()
        runner.consumed = []
        runner._consume_pending_turn_sidecar_notes = runner.consumed.append
        self.runner = runner
        return asyncio.run(runner._handle_message_with_agent(SimpleNamespace(**fields), source or _source(), 'q', 1))

    def test_owner_dm_greeting_bypasses_llm_with_exact_speak_this(self):
        self.handler.handle('gateway:startup', {})
        result = self._turn('Oi')
        self.assertEqual(result['final_response'], self.speak)
        self.assertEqual(result['api_calls'], 0)
        self.assertFalse(result['agent_persisted'])
        self.assertFalse(result['failed'])
        self.assertEqual(self.llm_calls, [])

    def test_prefixed_event_text_uses_owner_words(self):
        self.handler.handle('gateway:startup', {})
        result = self._turn('[Untrusted account data; x]\n\nolá', recall_text='olá')
        self.assertEqual(result['final_response'], self.speak)
        self.assertEqual(self.runner.consumed, ['k'])

    def test_media_quotes_and_goal_turns_reach_the_llm(self):
        self.handler.handle('gateway:startup', {})
        cases = (
            dict(text='[image note]\n\noi', recall_text='oi', media_urls=['/c/x.jpg'],
                 media_types=['image/jpeg'], message_type=SimpleNamespace(value='photo')),
            dict(text='[doc note]\n\noi', recall_text='oi', media_types=['text/plain'],
                 message_type=SimpleNamespace(value='document')),
            dict(text='[Untrusted quoted message; Quer que eu abra o PR?]\n\noi', recall_text='oi'),
            dict(text='[Standing goal, set by your owner with /goal ...]\n\noi', recall_text='oi'),
            dict(text='[Untrusted account data; x]\n\n[roster]\n\noi', recall_text='oi'),
        )
        for fields in cases:
            self.llm_calls.clear()
            self.assertEqual(self._turn(**fields)['final_response'], 'LLM reply', fields)
            self.assertEqual(len(self.llm_calls), 1)

    def test_plow_restart_wake_is_silent_without_llm(self):
        self.handler.handle('gateway:startup', {})
        wake = ("Plow, not your owner: you just came online in your owner's chat. "
                'This is a restart. If you have nothing to say, reply with exactly NO_REPLY.')
        result = self._turn(wake, _source(user_id='plow_setup'), recall_text=None)
        self.assertEqual(result['final_response'], 'NO_REPLY')
        self.assertEqual(result['api_calls'], 0)
        self.assertEqual(self.llm_calls, [])
        # Anything else from plow_setup (or a wake without the sentinel) runs normally.
        for text in ('Plow, not your owner: you just came online.', 'oi'):
            self.llm_calls.clear()
            self.assertEqual(self._turn(text, _source(user_id='plow_setup'))['final_response'], 'LLM reply')

    def test_everything_else_reaches_the_llm(self):
        self.handler.handle('gateway:startup', {})
        for text, source in (
            ('investiga a 42', _source()),
            ('oi', _source(chat_type='group')),
            ('oi', _source(role_authorized=False)),
            ('oi', _source(user_id='plow_setup')),
            ('oi', _source(user_id='plow_goal')),
            ('oi', _source(platform=_Platform('telegram'))),
        ):
            self.llm_calls.clear()
            self.assertEqual(self._turn(text, source)['final_response'], 'LLM reply', (text, source))
            self.assertEqual(len(self.llm_calls), 1)

    def test_stale_context_does_not_bypass(self):
        self.handler.handle('gateway:startup', {})
        result = self._turn('investiga a 42', recall_text='oi')
        self.assertEqual(result['final_response'], 'LLM reply')

    def test_status_failure_falls_back_to_llm(self):
        self.handler.handle('gateway:startup', {})
        with patch('watson.greeting.capabilities_report', side_effect=OSError('boom')), \
             self.assertLogs('watson.greet', level='ERROR'):
            result = self._turn('oi')
        self.assertEqual(result['final_response'], 'LLM reply')

    def test_install_is_idempotent_and_ignores_other_events(self):
        self.handler.handle('agent:start', {})
        self.assertFalse(self.mixin.__dict__.get('_watson_greet_patched', False))
        self.handler.handle('gateway:startup', {})
        patched = self.mixin._run_agent
        self.handler.handle('gateway:startup', {})
        self.assertIs(self.mixin._run_agent, patched)

    def test_unexpected_signature_leaves_gateway_untouched(self):
        async def _run_agent(self, prompt, **kwargs):
            return {}

        self.mixin._run_agent = _run_agent
        self.handler.handle('gateway:startup', {})
        self.assertIs(self.mixin._run_agent, _run_agent)
        self.assertFalse(self.mixin.__dict__.get('_watson_greet_patched', False))


class RuntimeConfigTests(unittest.TestCase):
    def test_mcp_env_passes_github_and_plow_tokens(self):
        text = (ROOT / 'runtime' / 'mcp-watson.yaml').read_text()
        env = text.split('    env:\n', 1)[1].split('    timeout:', 1)[0]
        self.assertIn('GH_TOKEN: ${GH_TOKEN}', env)
        self.assertIn('PLOW_AGENT_TOKEN: ${PLOW_AGENT_TOKEN}', env)
        self.assertIn('HOME: /var/lib/hermes', env)
        # Inherited PATH keeps /opt/hermes/.venv/bin for the OAuth waiter.
        self.assertIsNone(re.search(r'^\s+PATH:', env, re.M))

    def test_config_merge_is_idempotent_union(self):
        merge = _load(MERGE, 'watson_config_merge_under_test').merge
        watson = {'command': 'watson', 'env': {'GH_TOKEN': '${GH_TOKEN}'}}
        overlay = {'mcp_servers': {'watson': watson},
                   'skills': {'platform_disabled': {'plow_chat': ['github']}}}
        data = {'mcp_servers': {'plow': {'url': 'x'}, 'watson': {'command': 'old'}},
                'skills': {'platform_disabled': {'plow_chat': ['himalaya']}}}
        self.assertTrue(merge(data, overlay))
        self.assertEqual(data['mcp_servers'], {'plow': {'url': 'x'}, 'watson': watson})
        self.assertEqual(data['skills']['platform_disabled']['plow_chat'], ['himalaya', 'github'])
        self.assertFalse(merge(data, overlay))
        empty = {'mcp_servers': None, 'skills': None}
        self.assertTrue(merge(empty, overlay))
        self.assertEqual(empty['skills'], {'platform_disabled': {'plow_chat': ['github']}})


if __name__ == '__main__':
    unittest.main()
