"""Offline provider contract tests; these never call a paid API."""
import copy,json,unittest
try:
    from deepseek_matching import build_request,parse_response,cost_microusd
except ImportError:build_request=parse_response=cost_microusd=None

class DeepSeekTests(unittest.TestCase):
    text='Requirements\nPython experience required.\nRust preferred.'
    def response(self):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'requirements':[{'quote':'Python experience required.','importance':'mandatory','category':'skill','alternatives':['Python']},{'quote':'Rust preferred.','importance':'preferred','category':'skill','alternatives':['Rust']}]})}}], 'usage':{'prompt_tokens':100,'completion_tokens':50}}
    def test_request_contains_only_posting_data_and_no_tools(self):
        self.assertIsNotNone(build_request)
        request=build_request(self.text)
        self.assertEqual(request['model'],'deepseek-flash');self.assertEqual(request['response_format'],{'type':'json_object'})
        self.assertEqual(json.loads(request['messages'][1]['content']),{'untrusted_job_description':self.text})
        self.assertNotIn('tools',request);self.assertEqual(request['thinking'],{'type':'disabled'})
    def test_exact_quotes_have_offsets_but_do_not_certify_completeness(self):
        self.assertIsNotNone(parse_response)
        result=parse_response(self.response(),self.text)
        self.assertFalse(result['requirements_verified']);self.assertFalse(result['application_ready'])
        for row in result['requirements']:self.assertEqual(self.text[row['start']:row['end']],row['quote'])
        self.assertEqual(result['cost_microusd_peak_cache_miss'],90)
    def test_invented_quote_truncation_empty_or_extra_fields_are_refused(self):
        self.assertIsNotNone(parse_response)
        for change in ['quote','truncation','empty','extra','usage']:
            response=self.response();content=json.loads(response['choices'][0]['message']['content'])
            if change=='quote':content['requirements'][0]['quote']='Five years experience'
            elif change=='truncation':response['choices'][0]['finish_reason']='length'
            elif change=='extra':content['qualified']=True
            elif change=='usage':response['usage']['prompt_tokens']=-1
            response['choices'][0]['message']['content']='' if change=='empty' else json.dumps(content)
            with self.subTest(change=change),self.assertRaises(ValueError):parse_response(response,self.text)
    def test_bounds_and_duplicate_ambiguous_quotes_are_refused(self):
        self.assertIsNotNone(build_request)
        for text in ['',None,'x'*40001]:
            with self.assertRaises(ValueError):build_request(text)
        with self.assertRaises(ValueError):parse_response(self.response(),self.text+'\nPython experience required.')
    def test_peak_cost_rounds_up_and_never_trusts_boolean_usage(self):
        self.assertIsNotNone(cost_microusd)
        self.assertEqual(cost_microusd(1,1),2)
        for args in [(True,0),(-1,0),(1,2.3)]:
            with self.assertRaises(ValueError):cost_microusd(*args)

    def test_negated_requirement_cannot_be_used_as_positive_requirement(self):
        text='No prior Go experience is required.'
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'requirements':[{'quote':text,'importance':'mandatory','category':'experience','alternatives':[]}]})
        result=parse_response(response,text);row=result['requirements'][0]
        self.assertEqual(row['importance'],'unknown');self.assertIn('negated',row['interpretation_warning'])
        self.assertFalse(result['requirements_verified'])
