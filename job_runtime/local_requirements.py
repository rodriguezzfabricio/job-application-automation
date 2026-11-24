"""Extract simple, explicitly headed requirements. Unrecognized clauses go to the worker."""
import re

from .adapters import legacy
from .match_batch import hard_gate

US = re.compile(r'\bUnited States\b|\bUSA\b|\bUS[- ,/]+[A-Z]{2}\b|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b')
QUALIFICATIONS = re.compile(r'^\s*(?:required|basic|minimum) qualifications\s*:?\s*$\n(.*?)(?=^\s*(?:preferred qualifications|nice to have|what we offer|benefits)\s*:?\s*$)', re.I|re.M|re.S)
REVIEW = re.compile(r'\b(?:clearance|citizenship required|must be a (?:U\.?S\.? )?citizen|government contract|export control|ITAR|relocat\w+|security check|professional licen[cs]e|(?:bachelor|master|doctorat|Ph\.?D)\w*|(?:\d+\+?|one|two|three|four|five|six|seven|eight|nine|ten)\s+years?|repost(?:ed)?|no longer accepting)\b',re.I)
# Each entry is one capability. Alternatives come from the wording of the posting.
SKILLS = (
    ('data pipelines','data pipelines'),('data integration','data integration'),
    ('relational databases','relational databases'),('data warehouses','data warehouse'),
    ('large-scale data platforms','data platforms'),('data modeling','data modeling'),
    ('data quality','data quality'),('ETL','ETL'),('ELT','ELT'),('SQL','SQL'),
    ('Python','Python'),('Java','Java'),('Scala','Scala'),('JavaScript','JavaScript'),
    ('TypeScript','TypeScript'),('Kubernetes','Kubernetes'),('Docker','Docker'),
)

def extract(job, text, root=None):
    location = job.get('location') or ''
    role = legacy('title_filter').decide(job['title'],location)
    section = QUALIFICATIONS.search(text)
    if (not role['keep'] or role['role'] not in ('data','swe','ai') or not US.search(location)
            or not section or REVIEW.search(text) or hard_gate(text)[0]):
        return None
    outside = text[:section.start()] + '\n' + text[section.end():]
    conditional = r'(?:if you are made|upon receiving) a conditional offer of employment,? you (?:will be|are) required to undergo a drug test[.]?'
    if re.search(conditional,outside,re.I):
        from .terms import SCREENING_APPROVAL
        bank = root/'form-answers.md' if root else None
        if not bank or not bank.is_file() or SCREENING_APPROVAL not in bank.read_text():
            return None
        outside = re.sub(conditional,'',outside,flags=re.I)
    if re.search(r'\b(?:must|mandatory|minimum|required|applicants? (?:need|shall)|you (?:need to|will need))\b',outside,re.I):
        return None
    conditions = []
    reqs = []
    for raw in section[1].splitlines():
        line = raw.strip(' \t•-')
        if not line:
            continue
        if re.fullmatch(r'(?:strong|excellent) communication and collaboration skills[,. ]*(?:with the ability to work across technical and business teams)?[.]?',line,re.I):
            conditions.append(dict(quote=line,skills=['communication','collaboration']))
            continue
        body = re.sub(r'^(?:experience (?:building, supporting and troubleshooting|working with|with|in)|working knowledge of|knowledge of|proficiency in|understanding of)\s+', '', line, flags=re.I).rstrip('.')
        if body == line.rstrip('.'):
            return None
        body = re.sub(r'(?:a programming language commonly used for [\w ]+, such as|one of|either)\s+', '', body, flags=re.I)
        body = re.sub(r' (?:solutions|processes)$', '', body, flags=re.I)
        terms = {phrase.lower():term for phrase,term in SKILLS}
        groups = []
        for clause in re.split(r',?\s+and\s+', body, flags=re.I):
            alternatives = bool(re.search(r'\s+or\s+',clause,re.I))
            values = [re.sub(r'^other\s+', '',v.strip(),flags=re.I).lower()
                      for v in re.split(r',\s*(?:or\s+)?|\s+or\s+',clause,flags=re.I)]
            if any(v not in terms for v in values):
                return None
            group = [terms[v] for v in values]
            groups.extend([group] if alternatives else [[v] for v in group])
        synonyms = {'relational databases':('SQL','PostgreSQL','MySQL'), 'data warehouse':('data warehousing','Snowflake','BigQuery'),
                    'data modeling':('database design','schema design'),'data quality':('data validation',),
                    'ETL':('extract transform load',),'ELT':('extract load transform',)}
        for group in groups:
            options = list(dict.fromkeys(word for word in group))
            for term in group:
                options.extend(x for x in synonyms.get(term,()) if x not in options)
            reqs.append(dict(id='r'+str(len(reqs)+1),kind='must',label=' or '.join(group),quote=line,terms=options))
    if not reqs:
        return None
    return dict(track={'swe':'general'}.get(role['role'],role['role']),requirements=reqs,additional_conditions=conditions,gates=dict(role=role['role'],professional_years_required=0))
