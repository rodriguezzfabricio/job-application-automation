"""Conservative requirement evidence, never semantic/production certification.

Only explicit supported predicates can be marked met. Unsupported or compound
wording stays unknown; preferred gaps never become rejection reasons.
"""
import hashlib,re
from matching import candidate_context
from eligibility import evaluate_clause
from policy import fit,skill_key,skill_mentioned
from semantic_segments import VERSION,source_segments,WARNINGS,EXPLICIT,NEGATED


def _learning_language_evidence(profile,quote):
    token=r'[A-Za-z](?:[A-Za-z0-9_+#/-]|\.(?=[A-Za-z0-9])){0,60}'
    choices=token+r'(?:,? (?:or )?'+token+r')*'
    match=re.fullmatch(r'Working towards a proficiency of one or more programming languages such as ('+choices+r')[.]?', ' '.join(quote.split()),re.I)
    if not match:return None
    labels=re.split(r',\s*(?:or )?|\s+or\s+',match[1],flags=re.I)
    if any(not re.fullmatch(token,label) for label in labels):return None
    known={skill_key(label) for label in profile.get('skills',[])}
    supported=[label for label in labels if skill_key(label) in known]
    refs=[v['id'] for v in profile.get('variants',[]) if any(skill_key(label) in {skill_key(x) for x in v.get('skills',[])} and skill_mentioned(label,v['text']) for label in supported)]
    return ('met',refs,'Approved source examples support the stated learning-level language use; no advanced proficiency or production depth is claimed.') if refs else ('unknown',[],'No original-source example supports a named learning-level language; absence is not proof of inability.')


def _evaluate(profile,row,*,source_section=None):
    quote=row['quote'];lower=quote.casefold();category=row['category']
    state='unknown';refs=[];explanation='Requirement needs interpretation or additional verified evidence.'
    explicit=evaluate_clause(profile,quote)
    if row.get('interpretation_warning'):
        if explicit['predicate'] is not None and explicit['status'] in {'met','not_met'}:
            return explicit['status'],explicit['profile_evidence_ids'],explicit['explanation']+' Requirement classification remains uncertain: '+row['interpretation_warning']
        return state,refs,row['interpretation_warning']
    if explicit['predicate'] is not None:return explicit['status'],explicit['profile_evidence_ids'],explicit['explanation']
    if category=='skill':
        learning=_learning_language_evidence(profile,quote)
        if learning:return learning
        labels=row['alternatives'];known={skill_key(s) for s in profile.get('skills',[])}
        supported=[s for s in labels if skill_key(s) in known and any(skill_key(s) in {skill_key(x) for x in v.get('skills',[])} and skill_mentioned(s,v['text']) for v in profile.get('variants',[]))]
        refs=[v['id'] for v in profile.get('variants',[]) if any(skill_key(s) in {skill_key(x) for x in v.get('skills',[])} and skill_mentioned(s,v['text']) for s in supported)]
        escaped=[re.escape(a) for a in labels]
        label_pattern='(?:'+'|'.join(escaped)+')' if escaped else '(?!)'
        simple_pattern=r'(?:(?:experience|knowledge) (?:with|of|in) )?'+label_pattern+r'(?:\s+(?:and|or)\s+'+label_pattern+r')*(?: experience)? (?:is )?required[.!]?'
        simple=bool(re.fullmatch(simple_pattern,quote.strip(),re.I))
        # The heading supplies mandatory scope, never proficiency or duration.
        # Full-string matching binds every word to the source skill inventory;
        # a model-selected subset cannot erase another skill or condition.
        if source_section=='mandatory' and row['importance']=='mandatory':
            heading_pattern=r'(?:experience|knowledge) (?:with|of|in) '+label_pattern+r'(?:\s+(?:and|or)\s+'+label_pattern+r')*[.!]?'
            simple=simple or bool(re.fullmatch(heading_pattern,quote.strip(),re.I))
        advanced=bool(re.search(r'\b(strong|expert|expertise|proficien\w*|solid|production|professional|commercial|years?)\b',lower))
        conjunction=bool(re.search(r'\band\b',lower));disjunction=bool(re.search(r'\bor\b|one or more|at least one',lower))
        if advanced:explanation='Your résumé lists relevant skills, but does not establish the required depth or all conditions in this sentence.'
        elif conjunction and disjunction:explanation='This sentence mixes skill choices and other conditions; each needs a separate check.'
        elif simple and labels and ((disjunction and supported) or (not disjunction and len(supported)==len(labels))):
            state='met';explanation='Your master résumé supports the stated skill requirement.'
        else:explanation='Your résumé does not establish all conditions in this sentence; additional or compound requirements remain uncertain.'
    elif category=='experience':
        count=profile.get('internships_count')
        if re.fullmatch(r'previous internship experience (?:is )?required[.!]?',lower.strip()) and type(count) is int and count>=1:
            state='met';refs=['profile:internships_count'];explanation='Your verified profile confirms prior internship experience; no duration is inferred.'
        else:explanation='Internship count does not establish employment duration, seniority, startup context or full-time years.'
    elif category=='authorization' and re.fullmatch(r'(?:must be authorized to work in (?:the united states|the us|the u\.s\.)|(?:us|u\.s\.) work authorization (?:is )?required)[.!]?',lower.strip()):
        authorized=profile.get('work_authorized_us')
        if type(authorized) is bool:
            state='met' if authorized else 'not_met';refs=['profile:work_authorized_us'];explanation='Uses explicitly confirmed US work authorization only; no citizenship, clearance or future authorization guarantee is inferred.'
        else:explanation='US work authorization is unconfirmed; no-sponsorship-needed is not authorization evidence.'
    elif category=='authorization' and re.fullmatch(r'(?:us|u\.s\.) citizenship (?:is )?required[.!]?',lower.strip()):
        citizenship=profile.get('us_citizen')
        if type(citizenship) is bool:
            state='met' if citizenship else 'not_met';refs=['profile:us_citizen'];explanation='Uses explicitly confirmed citizenship only.'
        else:explanation='Citizenship is unconfirmed; no-sponsorship-needed is not citizenship evidence.'
    elif category in {'education','timing'}:
        if re.search(r'\b(winter|spring|summer|fall|autumn)\b',lower):explanation='Employer season boundaries are not defined; no season-to-month inference is made.'
        else:explanation='Education or timing wording needs a supported degree/window predicate; month precision is retained.'
        refs=['profile:education','profile:earliest_start']
    elif category=='location':
        refs=['profile:locations','profile:willing_to_relocate']
        explanation='Compare all stated locations, work arrangements and embedded start conditions before qualifying; relocation alone is insufficient.'
    elif category=='contracting':explanation='Apply the role-specific government-contracting exclusion, independent of preferred/mandatory labels.'
    return state,list(dict.fromkeys(refs)),explanation


def _validate_warning(row,span):
    warning=row.get('interpretation_warning')
    if warning is None:return
    if not isinstance(warning,str) or warning not in WARNINGS.values() or row.get('importance')!='unknown':raise ValueError('invalid interpretation warning')
    if warning==WARNINGS['required'] and span['section']!='mandatory':raise ValueError('unbound requirement warning')
    if warning==WARNINGS['negated'] and not NEGATED.search(span['text']):raise ValueError('unbound negation warning')
    if warning==WARNINGS['omission'] and (span['is_heading'] or not EXPLICIT.search(span['text']) or row.get('category')!='other'):raise ValueError('unbound omission warning')
    if warning==WARNINGS['preferred'] and span['section']!='preferred' and not re.search(r'\b(preferred|nice.to.have|is a plus|bonus)\b',span['text'],re.I):raise ValueError('unbound preferred warning')
    if warning==WARNINGS['unsupported'] and (row.get('category')!='other' or row.get('alternatives')!=[]):raise ValueError('unbound unsupported-label warning')


def inspect_fit(profile,job,extraction,*,approval_sha256):
    context=candidate_context(profile,approval_sha256=approval_sha256)
    description=job.get('description')
    if not isinstance(description,str) or not isinstance(extraction,dict) or extraction.get('schema_version')!=VERSION or extraction.get('description_sha256')!=hashlib.sha256(description.encode()).hexdigest():raise ValueError('extraction/source mismatch')
    source={r['id']:r for r in source_segments(description)}
    classified=extraction.get('classified_segments');requirements=extraction.get('requirements')
    if not isinstance(classified,list) or not isinstance(requirements,list) or len(classified)!=len(source):raise ValueError('incomplete classified evidence')
    seen=set()
    for row in classified:
        identifier=row.get('id');span=source.get(identifier)
        if span is None or identifier in seen or row.get('start')!=span['start'] or row.get('end')!=span['end'] or row.get('quote')!=span['text']:raise ValueError('unbound source segment')
        seen.add(identifier)
    expected=[r for r in classified if r.get('category')!='none']
    if requirements!=expected:raise ValueError('requirement/classification mismatch')
    rows=[]
    for row in requirements:
        if row.get('importance') not in {'mandatory','preferred','unknown'} or not isinstance(row.get('alternatives'),list):raise ValueError('invalid requirement schema')
        if any(not isinstance(a,str) or not a.strip() or not skill_mentioned(a,row['quote']) for a in row['alternatives']):raise ValueError('unsupported skill label')
        _validate_warning(row,source[row['id']])
        status,refs,explanation=_evaluate(context,row,source_section=source[row['id']]['section'])
        rows.append({'requirement_id':row['id'],'source_start':row['start'],'source_end':row['end'],'job_requirement':row['quote'],'importance':row['importance'],'category':row['category'],'status':status,'profile_evidence_ids':refs,'explanation':explanation})
    base=fit(context,dict(job,requirements_status='unreviewed'))
    failed=[r for r in rows if r['importance']=='mandatory' and r['status']=='not_met']
    preferred_misses=[r for r in rows if r['importance']=='preferred' and r['status']=='not_met']
    possible=[r for r in rows if r['importance']=='unknown' and r['status']=='not_met']
    return {'status':'reject' if base['status']=='reject' or failed else 'review','reasons':list(dict.fromkeys(base['reasons']+['Confirmed requirement not met: '+r['job_requirement'] for r in failed]+['Possible requirement mismatch needs review: '+r['job_requirement'] for r in possible]+['Preferred requirement not met: '+r['job_requirement'] for r in preferred_misses]+['semantic classification needs validated full-path evaluation'])),'evidence_rows':rows,
            'profile_hash':approval_sha256,'description_sha256':extraction['description_sha256'],'extraction_version':VERSION,'requirements_verified':False,'application_ready':False}
