import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from job_runtime.answers import validate_answers
from job_runtime.store import Denied

class AnswerTests(unittest.TestCase):
    def test_saved_literal_and_invented_value(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'facts.md').write_text('Full name: Pat Example. Professional experience: 0 years.')
            row={'field':'Name','value':'Pat Example','kind':'literal','source':'facts.md','quote':'Full name: Pat Example.','refs':['fixture:observed-field']}
            self.assertTrue(validate_answers(d,[row]))
            with self.assertRaises(Denied):validate_answers(d,[dict(row,value='Invented Name')])
    def test_missing_answer_evidence_blocks(self):
        with self.assertRaises(Denied):validate_answers('/unused',[])
    def private(self,d):
        from job_runtime.terms import MARKERS
        from job_runtime.answers import RULES
        markers = [marker for source, marker in RULES.values() if source == 'form-answers.md']
        markers.extend(MARKERS.values())
        Path(d,'form-answers.md').write_text('\n'.join(markers).replace('Salary with no posted range:', 'Salary with no posted range: 84000').replace('Years of full-time professional experience:', 'Years of full-time professional experience: 2'))
        Path(d,'facts.md').write_text('')
        stages=Path(d,'stages')
        stages.mkdir()
        (stages/'one-click.md').write_text('\n'.join(marker for source,marker in RULES.values() if source.startswith('stage:')))
    def rule(self,rule,field,value,**extra):
        return dict({'field':field,'value':value,'kind':'rule','rule':rule,'refs':['fixture:observed-field']},**extra)
    def test_owner_rules_answer_without_stopping(self):
        with tempfile.TemporaryDirectory() as d:
            self.private(d)
            if not Path(d,'form-answers.md').read_text():self.skipTest('private form-answers.md not present')
            rows=[self.rule('default_yes','Are you comfortable working in a fast-paced environment?','Yes'),
                  self.rule('sms_consent_yes','Can we text you about this application?','Yes'),
                  self.rule('legal_ack','I agree to the arbitration agreement','checked'),
                  self.rule('years_rule','Years of professional experience','2'),
                  self.rule('salary_default','Desired salary','84000'),
                  self.rule('rating_rule','Rate your Python skill (1-5)','3'),
                  self.rule('choice_rule','Which team interests you most?','Platform')]
            self.assertTrue(validate_answers(d,rows))
    def test_rules_never_answer_identity_legal_signature_or_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            self.private(d)
            for field in ('Are you a U.S. citizen?','Do you hold a security clearance?','Have you been convicted of a felony?',
                          'Date of birth','Will you require visa sponsorship?','Electronic signature','Password','Veteran status'):
                with self.assertRaises(Denied,msg=field):validate_answers(d,[self.rule('default_yes',field,'Yes')])
    def test_rule_limits(self):
        with tempfile.TemporaryDirectory() as d:
            self.private(d)
            for row in (self.rule('years_rule','Years of experience','7'),
                        self.rule('salary_default','Desired salary','250000'),
                        self.rule('free_text_rule','Why us?','word '*151,claims_checked=True,claim_quotes=['x']),
                        self.rule('free_text_rule','Why us?','Short answer.'),
                        self.rule('made_up_rule','Anything','Yes'),
                        dict(self.rule('default_yes','Q','Yes'),refs=[])):
                with self.assertRaises(Denied):validate_answers(d,[row])
    def test_rule_needs_its_marker_on_record(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d,'form-answers.md').write_text('nothing relevant');Path(d,'facts.md').write_text('')
            with self.assertRaises(Denied):validate_answers(d,[self.rule('default_yes','Q','Yes')])
    def test_prefilled_must_be_preserved(self):
        with self.assertRaises(Denied):validate_answers('/unused',[{'field':'Years','value':'3','kind':'prefilled','observed_value':'0','preserved':True,'refs':['fixture:field']}])

if __name__=='__main__':unittest.main()



class PrivateBindingsTests(unittest.TestCase):
    def test_binding_requires_exact_question_value_and_saved_source(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            question = 'Preferred shift?'
            entry = {'meaning':'shift','field':question,'value':'Morning','quote':'Preferred shift: Morning'}
            (p/'form-options.json').write_text(json.dumps([entry]))
            (p/'form-answers.md').write_text(entry['quote'])
            item = {'field':question,'value':'Morning','kind':'plain','meaning':'shift','refs':['fixture:shift']}
            self.assertTrue(validate_answers(p, [item]))
            for changes in ({'field':'Start time?'}, {'value':'Evening'}, {'meaning':'unknown'}):
                with self.assertRaises(Denied):
                    validate_answers(p, [dict(item, **changes)])
            (p/'form-answers.md').write_text('')
            with self.assertRaises(Denied):
                validate_answers(p, [item])
