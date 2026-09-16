import unittest
from unittest.mock import Mock
from watson.captions import narrate, srt_text
from watson.core import WatsonError

class CaptionTests(unittest.TestCase):
    def test_timing_order_and_plain_text(self):
        events=[{'start':1.234,'end':3.456},{'start':3.5,'end':6}]
        value=srt_text(events,['Testing {\\pos(0,0)} <b>form</b>\nnow','Título rejeitado corretamente.'])
        self.assertIn('00:00:01,234 --> 00:00:03,456',value)
        self.assertNotIn('{',value); self.assertNotIn('\\',value); self.assertNotIn('<',value)
        self.assertIn('Título rejeitado',value)
        with self.assertRaises(WatsonError):
            srt_text([{'start':2,'end':4},{'start':3,'end':5}],['a','b'])

    def test_model_cannot_add_events_or_change_timing(self):
        model=Mock(); model.ask.return_value={'language':'en','captions':['extra','extra']}
        with self.assertRaises(WatsonError):
            narrate(model,{'title':'Issue','body':'Example','number':1},[{'start':0,'end':3,'event':'Ready'}])

    def test_captions_preserve_issue_language_and_event_alignment(self):
        model=Mock(); model.ask.return_value={'language':'en','captions':['The title is rejected.']}
        events=[{'start':2,'end':5,'event':{'status':'passed'}}]
        result=narrate(model,{'title':'English issue','body':'Expected behavior','number':1},events)
        self.assertEqual(result['language'],'en')
        self.assertEqual(model.ask.call_args.args[1]['events'],events)
