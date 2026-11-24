import copy,json,unittest
try:
    from semantic_segments import source_segments,build_segment_request,parse_segment_response
except ImportError:source_segments=build_segment_request=parse_segment_response=None

class SegmentTests(unittest.TestCase):
    text='Requirements:\r\n  Python required.  \r\nRust preferred.\nNo Go experience is required.\n'
    def response(self):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Python']],['s2','skill','preferred',['Rust']],['s3','experience','mandatory',[]]]})}}],'usage':{'prompt_tokens':100,'completion_tokens':100}}
    def test_exact_source_offsets_survive_whitespace_unicode_and_repeated_lines(self):
        self.assertIsNotNone(source_segments)
        text='  Café required.\r\n  Café required.  \n'
        rows=source_segments(text);self.assertEqual(len(rows),2)
        for row in rows:self.assertEqual(text[row['start']:row['end']],row['text'])
        self.assertNotEqual(rows[0]['id'],rows[1]['id'])
    def test_request_has_only_numbered_job_text(self):
        self.assertIsNotNone(build_segment_request)
        request=build_segment_request(self.text);body=json.loads(request['messages'][1]['content'])
        self.assertEqual(len(body['untrusted_job_segments']),4);self.assertEqual(body['untrusted_job_segments'][1],{'id':'s1','text':'Python required.'})
        self.assertNotIn('tools',request)
    def test_all_segments_grounded_but_semantics_remain_unverified(self):
        self.assertIsNotNone(parse_segment_response)
        result=parse_segment_response(self.response(),self.text)
        self.assertTrue(result['segment_coverage_complete']);self.assertFalse(result['requirements_verified']);self.assertFalse(result['application_ready'])
        self.assertEqual(len(result['requirements']),3)
        for row in result['requirements']:self.assertEqual(self.text[row['start']:row['end']],row['quote'])
        self.assertEqual(result['requirements'][-1]['importance'],'unknown')
    def test_omitted_duplicate_or_unknown_segments_are_refused(self):
        self.assertIsNotNone(parse_segment_response)
        for change in ['omit','duplicate','unknown']:
            response=self.response();body=json.loads(response['choices'][0]['message']['content'])
            if change=='omit':body['segments'].pop()
            elif change=='duplicate':body['segments'][-1][0]='s1'
            else:body['segments'][-1][0]='s99'
            response['choices'][0]['message']['content']=json.dumps(body)
            with self.subTest(change=change),self.assertRaises(ValueError):parse_segment_response(response,self.text)
    def test_none_cannot_hide_explicit_requirement_and_invented_skill_never_accepted(self):
        self.assertIsNotNone(parse_segment_response)
        for change in [['s1','none','unknown',[]],['s1','skill','mandatory',['Java']]]:
            response=self.response();body=json.loads(response['choices'][0]['message']['content']);body['segments'][1]=change
            response['choices'][0]['message']['content']=json.dumps(body)
            if change[1]=='none':
                result=parse_segment_response(response,self.text);self.assertEqual(result['requirements'][0]['importance'],'unknown')
            else:
                result=parse_segment_response(response,self.text);row=result['requirements'][0]
                self.assertEqual((row['category'],row['importance'],row['alternatives']),('other','unknown',[]));self.assertIn('unsupported',row['interpretation_warning'])
    def test_limits_truncation_and_extra_output_fields_are_refused(self):
        self.assertIsNotNone(source_segments)
        for text in ['',None,'x'*40001,'\n'.join('line' for _ in range(81))]:
            with self.assertRaises(ValueError):source_segments(text)
        response=self.response();response['choices'][0]['finish_reason']='length'
        with self.assertRaises(ValueError):parse_segment_response(response,self.text)

    def test_required_section_cannot_silently_drop_or_downgrade_bullets(self):
        text='''What You Have
Communicate clearly with your team.
Comfortable using Cursor.
Startup experience is a plus.'''
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','none','unknown',[]],['s2','skill','preferred',['Cursor']],['s3','experience','preferred',[]]]})
        result=parse_segment_response(response,text)
        self.assertEqual([r['id'] for r in result['requirements']],['s1','s2','s3'])
        self.assertEqual([r['importance'] for r in result['requirements']],['unknown','unknown','unknown'])
        self.assertTrue(all(r.get('interpretation_warning') for r in result['requirements']))

    def test_explicit_optional_prefix_has_local_scope_inside_required_section(self):
        for marker in ['nice-to-have','nice\u2011to\u2011have','preferred']:
            text="What You'll Bring:\nAI experience ("+marker+"): Hands-on with Rust."
            response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','preferred',['Rust']]]})
            with self.subTest(marker=marker):
                row=parse_segment_response(response,text)['requirements'][0]
                self.assertEqual(row['importance'],'preferred');self.assertNotIn('interpretation_warning',row)
                self.assertEqual(text[row['start']:row['end']],row['quote'])

    def test_optional_prefix_cannot_hide_hard_wording_or_an_omitted_skill(self):
        for quote,category,importance,skills in [('AI experience (nice-to-have): Python required.','skill','preferred',['Python']),('Python required; AI experience (nice-to-have): Rust.','skill','preferred',['Rust']),('AI experience (nice-to-have): Rust.','none','unknown',[])]:
            text='Requirements\n'+quote;response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1',category,importance,skills]]})
            with self.subTest(quote=quote,category=category):
                row=parse_segment_response(response,text)['requirements'][0]
                self.assertEqual(row['importance'],'unknown');self.assertIn('interpretation_warning',row)

    def test_optional_prefix_cannot_hide_exclusion_or_other_eligibility_conditions(self):
        for quote,category,skills in [('Kubernetes (nice-to-have): applicants without production experience will not be considered.','skill',['Kubernetes']),('Qualifications (preferred): 3+ years Python, BS in Computer Science, US citizenship.','skill',['Python']),('Work authorization (preferred): we do not sponsor visas.','authorization',[]),('Security clearance (preferred): active TS/SCI is a condition of employment.','authorization',[])]:
            response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1',category,'preferred',skills]]})
            with self.subTest(quote=quote):
                row=parse_segment_response(response,'Requirements\n'+quote)['requirements'][0]
                self.assertEqual(row['importance'],'unknown');self.assertIn('interpretation_warning',row)

    def test_language_prefix_is_not_evidence_for_a_different_language(self):
        for quote,label in [('JavaScript required.','Java'),('C++ required.','C')]:
            response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','skill','mandatory',[label]]]})
            with self.subTest(quote=quote):
                row=parse_segment_response(response,quote)['requirements'][0]
                self.assertEqual(row['alternatives'],[]);self.assertEqual(row['importance'],'unknown');self.assertIn('unsupported',row['interpretation_warning'])

    def test_common_required_headings_share_one_canonical_classifier(self):
        for heading in ['Required Skills:','Basic Qualifications:','Must Haves:',"What we're looking for:","What we\u2019re looking for:"]:
            text=heading+'\nPython required.';response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Python']]]})
            with self.subTest(heading=heading):
                self.assertTrue(source_segments(text)[0]['is_heading']);self.assertEqual(source_segments(text)[1]['section'],'mandatory');self.assertEqual(len(parse_segment_response(response,text)['requirements']),1)
    def test_observed_company_and_future_work_headings_reset_required_scope(self):
        for heading in ['Who We Are',"What You'll Achieve:","What You\u2019ll Achieve:"]:
            text='Requirements\nPython required.\n'+heading+'\nYou will build Rust services.';response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Python']],['s2','none','unknown',[]],['s3','none','unknown',[]]]})
            with self.subTest(heading=heading):
                rows=source_segments(text);self.assertTrue(rows[2]['is_heading']);self.assertIsNone(rows[3]['section']);self.assertEqual([x['id'] for x in parse_segment_response(response,text)['requirements']],['s1'])
    def test_future_duty_is_not_promoted_to_past_skill_by_model(self):
        text='What you will do\nWrite C++, Python and Rust\nRequirements\nPython required.';response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['C++','Python','Rust']],['s2','none','unknown',[]],['s3','skill','mandatory',['Python']]]})
        result=parse_segment_response(response,text)
        self.assertEqual([x['id'] for x in result['requirements']],['s3']);self.assertEqual(result['classified_segments'][1]['quote'],'Write C++, Python and Rust')
        for condition in ['Experience writing Rust required.','Must write Rust from day one.','Work authorization required.']:
            changed=text.replace('Write C++, Python and Rust',condition);raw=self.response();raw['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','none','unknown',[]],['s2','none','unknown',[]],['s3','skill','mandatory',['Python']]]})
            with self.subTest(condition=condition):self.assertIn('s1',[x['id'] for x in parse_segment_response(raw,changed)['requirements']])
        for condition in ['Write Rust; successful applicants pass a technical screening.','Build Rust services, proficiency is a prerequisite.']:
            changed=text.replace('Write C++, Python and Rust',condition);raw=self.response();raw['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','skill','mandatory',['Rust']],['s2','none','unknown',[]],['s3','skill','mandatory',['Python']]]})
            with self.subTest(condition=condition):self.assertIn('s1',[x['id'] for x in parse_segment_response(raw,changed)['requirements']])
    def test_unheaded_sponsorship_and_experience_cannot_disappear_as_context(self):
        for text in ['We do not offer visa sponsorship.','Candidates need 3+ years of production Python.','PhD only.']:
            response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]]]})
            with self.subTest(text=text):
                result=parse_segment_response(response,text);self.assertEqual(len(result['requirements']),1)
                self.assertEqual(result['requirements'][0]['importance'],'unknown')

    def test_explicit_preferred_wording_cannot_be_promoted_to_mandatory(self):
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','skill','mandatory',['Rust']]]})
        result=parse_segment_response(response,'Rust is preferred.')
        self.assertEqual(result['requirements'][0]['importance'],'unknown')

    def test_bounded_68_line_description_keeps_every_source_id(self):
        text='\n'.join('Line '+str(n) for n in range(68))
        rows=source_segments(text);self.assertEqual(len(rows),68)
        request=build_segment_request(text);self.assertEqual(request['max_tokens'],2000)
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[[r['id'],'none','unknown',[]] for r in rows]})
        result=parse_segment_response(response,text)
        self.assertEqual(len(result['classified_segments']),68);self.assertFalse(result['requirements_verified'])
    def test_one_unsupported_label_withholds_entire_mixed_skill_row(self):
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','skill','mandatory',['Python','Rust']]]})
        result=parse_segment_response(response,'Python required.');row=result['requirements'][0]
        self.assertEqual((row['quote'],row['category'],row['importance'],row['alternatives']),('Python required.','other','unknown',[]))
    def test_malformed_alternatives_still_fail_closed(self):
        for alternatives in [None,'Python',['x'*101],[1]]:
            response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','skill','mandatory',alternatives]]})
            with self.subTest(alternatives=alternatives),self.assertRaises(ValueError):parse_segment_response(response,'Python required.')

    def test_notion_requirement_and_optional_headings_retain_scope(self):
        text="Skills You'll Need to Bring:\nCommunicate clearly with engineers.\nNice to Haves:\nRust experience."
        response=self.response();response['choices'][0]['message']['content']=json.dumps({'segments':[['s0','none','unknown',[]],['s1','none','unknown',[]],['s2','none','unknown',[]],['s3','skill','preferred',['Rust']]]})
        result=parse_segment_response(response,text)
        self.assertEqual([r['id'] for r in result['requirements']],['s1','s3'])
        self.assertEqual([r['importance'] for r in result['requirements']],['unknown','preferred'])
        rows=source_segments(text);self.assertEqual(rows[3]['section'],'preferred')
