import io
import json
import tempfile
import unittest
from pathlib import Path

from watson.core import Store, private_json
from watson.mcp import dispatch, serve


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        store = Store(self.home)
        store.db.close()
        private_json(self.home / 'config.json', {'repository': 'demo/repo', 'assignee': 'owner', 'related_repositories': []})

    def test_handshake_and_read_only_tool_surface(self):
        source = io.StringIO('\n'.join(json.dumps(x) for x in [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'watson_status', 'arguments': {}}},
        ]))
        output = io.StringIO()
        serve(self.home, source, output)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({t['name'] for t in messages[1]['result']['tools']}, {'watson_status', 'watson_investigate'})
        self.assertFalse(messages[2]['result']['isError'])

    def test_arbitrary_actions_and_repositories_rejected(self):
        for name, arguments in [('merge', {}), ('deliver', {}),
                                ('watson_investigate', {'number': 1, 'repo': 'other/repo'}),
                                ('watson_investigate', {'number': '1; echo bad'}),
                                ('watson_investigate', {'number': True})]:
            response = dispatch(self.home, {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
            self.assertTrue(response['isError'])


if __name__ == '__main__':
    unittest.main()
