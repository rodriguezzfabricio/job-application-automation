"""Pure policy helpers. No network, model, credentials or application submission."""
from datetime import datetime, timezone, date
import calendar
import math
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import re
import uuid
from itertools import combinations

POLICY_VERSION='sample-policy-3'

class TextOnly(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
        if tag in ('p','li','br','div'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.skip:self.skip-=1
        if tag in ('p','li','div'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

def text_only(value):
    if not isinstance(value,str):raise ValueError('text required')
    parser=TextOnly();parser.feed(unescape(unescape(value)))
    return '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines() if line.strip())

def instant(value):
    if not isinstance(value,str):raise ValueError('aware ISO timestamp required')
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('naive timestamp')
    try:return d.astimezone(timezone.utc)
    except OverflowError:raise ValueError('timestamp outside supported UTC range') from None

def budget_allowed(cap_microusd,spent_microusd,reserved_microusd,request_microusd):
    values=(cap_microusd,spent_microusd,reserved_microusd,request_microusd)
    return all(type(x) is int and x>=0 for x in values) and sum(values[1:])<=values[0]

def validate_selection(approved,selected):
    if not isinstance(approved,list) or not isinstance(selected,list) or not selected:return False
    try:
        ids=[x['id'] for x in approved]
        return all(isinstance(x,str) and x for x in ids+selected) and len(set(ids))==len(ids) and len(set(selected))==len(selected) and set(selected)<=set(ids) and all(isinstance(x.get('text'),str) and x['text'].strip() and skill_labels(x.get('skills',[])) for x in approved)
    except (KeyError,TypeError):return False

def novelty(known_ids,incoming,bootstrap,now):
    id=incoming.get('id')
    if not id or str(id) in {str(x) for x in known_ids}:return False
    if incoming.get('published_at') is not None:
        try:
            if instant(incoming['published_at'])>instant(now):return False
        except (ValueError,TypeError):return False
    if not bootstrap:return True
    if incoming.get('publication_semantics')!='original':return False
    try:age=(instant(now)-instant(incoming.get('published_at'))).total_seconds()
    except (ValueError,TypeError):return False
    return 0<=age<=3600

def norm(s):return re.sub(r'[^a-z0-9]+',' ',str(s).casefold()).strip()

def skill_key(s):
    # Technology punctuation is semantic: C, C++ and C# are different evidence.
    return ' '.join(str(s).casefold().split())

def skill_labels(value):
    return isinstance(value,list) and all(isinstance(s,str) and bool(s.strip()) for s in value)

def validate_skill_evidence(profile=None,job=None):
    """Single schema boundary shared by matching and every public document path."""
    if profile is not None:
        if not isinstance(profile,dict) or not skill_labels(profile.get('skills',[])):raise ValueError('invalid skill evidence schema')
        bank=profile.get('variants',[])
        if not isinstance(bank,list) or any(not isinstance(v,dict) or not skill_labels(v.get('skills',[])) for v in bank):raise ValueError('invalid skill evidence schema')
    if job is not None:
        if not isinstance(job,dict) or any(not skill_labels(job.get(k,[])) for k in ['required_skills','preferred_skills']):raise ValueError('invalid skill evidence schema')
        groups=job.get('required_skill_groups',[])
        if not isinstance(groups,list) or any(not skill_labels(g) or not g for g in groups):raise ValueError('invalid skill evidence schema')

def skill_mentioned(label,text):
    # Adjacent language punctuation is part of the token, never evidence for C.
    return bool(re.search(r'(?<![\w+#])'+re.escape(skill_key(label))+r'(?![\w+#])',' '.join(text.casefold().split())))

def skill_evidence(profile,job):
    """Explain structured skill predicates; absence of evidence is unknown."""
    validate_skill_evidence(profile,job);available={skill_key(s) for s in profile.get('skills',[])};rows=[]
    predicates=[('mandatory',[s]) for s in job.get('required_skills',[])]+[('mandatory',g) for g in job.get('required_skill_groups',[])]+[('preferred',[s]) for s in job.get('preferred_skills',[])]
    for importance,alternatives in predicates:
        matched={skill_key(s) for s in alternatives}&available
        ids=[v['id'] for v in profile.get('variants',[]) if matched&{skill_key(s) for s in v.get('skills',[])}]
        rows.append({'alternatives':alternatives,'importance':importance,'status':'met' if matched else 'unknown','matched_labels':sorted(matched),'evidence_ids':ids,'explanation':'Approved skill labels support this predicate.' if matched else 'No approved evidence; this does not prove lack of skill.'})
    return rows

def selection_covers_requirements(profile,job,selected):
    validate_skill_evidence(profile,job)
    bank={v['id']:v for v in profile.get('variants',[])}
    if any(id not in bank for id in selected):return False
    covered={skill_key(s) for id in selected for s in bank[id].get('skills',[])}&{skill_key(s) for s in profile.get('skills',[])}
    predicates=[[s] for s in job.get('required_skills',[])]+job.get('required_skill_groups',[])
    return all(covered&{skill_key(s) for s in group} for group in predicates)

def select_required_variants(profile,job,limit=2):
    validate_skill_evidence(profile,job)
    required={skill_key(s) for s in job.get('required_skills',[])}|{skill_key(s) for g in job.get('required_skill_groups',[]) for s in g}
    bank=sorted(profile.get('variants',[]),key=lambda v:v['id'])
    if len(bank)>30 or limit!=2:raise ValueError('unsupported bounded archive selection')
    relevant=[v['id'] for v in bank if required&{skill_key(s) for s in v.get('skills',[])}]
    for n in range(1,min(limit,len(relevant))+1):
        for ids in combinations(relevant,n):
            if selection_covers_requirements(profile,job,ids):return list(ids)
    return []

# Explicit aliases; country checks happen before US state/city mapping.
ALIASES={
 'brooklyn ny':'nyc','manhattan ny':'nyc','bronx ny':'nyc','queens ny':'nyc','staten island ny':'nyc','kirkland wa':'kirkland','cambridge ma':'cambridge','somerville ma':'somerville','waltham ma':'waltham','burlington ma':'burlington','gaithersburg md':'gaithersburg','new york city ny':'nyc','new york city new york':'nyc','nyc ny':'nyc','arlington va':'arlington','arlington virginia':'arlington','reston va':'reston','alexandria va':'alexandria',
 'new york':'nyc','new york city':'nyc','nyc':'nyc','new york ny':'nyc','new york new york':'nyc',
 'san francisco':'sf','san francisco ca':'sf','san francisco california':'sf','sf':'sf','bay area':'sf','san francisco bay area':'sf',
 'seattle':'seattle','seattle wa':'seattle','seattle washington':'seattle',
 'boston':'boston','boston ma':'boston','boston massachusetts':'boston',
 'jersey city':'jersey city','jersey city nj':'jersey city','jersey city new jersey':'jersey city',
 'chicago':'chicago','chicago il':'chicago','chicago illinois':'chicago',
 'washington dc':'dc','washington d c':'dc','district of columbia':'dc','dmv':'dmv',
 'austin':'austin','austin tx':'austin','austin texas':'austin','dallas tx':'dallas','dallas texas':'dallas',
 'miami fl':'miami','miami florida':'miami','miami':'miami',
 'los angeles ca':'la','los angeles california':'la','los angeles':'la','san diego ca':'sd','san diego california':'sd',
 'san jose ca':'sj','san jose california':'sj','san jose':'sj',
 'bethesda md':'bethesda','bethesda oregon':'bethesda','rockville md':'rockville','silver spring md':'silver spring','mclean va':'mclean','tysons va':'tysons','bellevue wa':'bellevue','redmond wa':'redmond','bellevue':'bellevue','redmond':'redmond',
 'california':'california','ca':'california','texas':'texas','tx':'texas','florida':'florida','fl':'florida','new jersey':'new jersey','nj':'new jersey'}

def geo(s):
    n=norm(s)
    if re.search(r'\b(on|ontario|bc|british columbia|qc|quebec) ca$',n):return None
    if any(re.search(r'\b'+x+r'\b',n) for x in ['canada','india','united kingdom','remote']):return None
    for country in ['united states of america','united states','usa','us']:n=re.sub(r'\b'+country+r'\b','',n).strip()
    n=' '.join(n.split())
    if n in ALIASES:return ALIASES[n]
    match=re.fullmatch(r'(.+) (california|ca|texas|tx|florida|fl|new jersey|nj)',n)
    if match:
        state={'ca':'california','tx':'texas','fl':'florida','nj':'new jersey'}.get(match[2],match[2]);return state+':'+match[1]
    return None

def location_matches(location,locations):
    if not isinstance(location,str) or any(x in location for x in [';',' / ',' | ']):return False
    city=geo(location);desired={geo(x) for x in locations};desired.discard(None)
    if not city:return False
    if city in desired:return True
    if ':' in city and city.split(':',1)[0] in desired:return True
    groups={'california':{'sf','la','sd','sj'},'texas':{'austin','dallas','houston','san antonio'},'florida':{'miami','tampa','orlando'},'new jersey':{'jersey city','newark'},'dmv':{'dc','arlington','reston','alexandria','bethesda','rockville','silver spring','mclean','tysons','gaithersburg'},'seattle':{'bellevue','redmond','kirkland'},'boston':{'cambridge','somerville','waltham','burlington'}}
    return any(state in desired and city in members for state,members in groups.items())

def _month_bounds(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}',value):raise ValueError('month required')
    y,m=map(int,value.split('-'));return date(y,m,1),date(y,m,calendar.monthrange(y,m)[1])

def _date_constraints(profile,job,reject,review):
    start=job.get('start_date')
    if start is not None:
        try:
            target=date.fromisoformat(start)
            if profile.get('available_from_date'):
                low=high=date.fromisoformat(profile['available_from_date'])
            else:
                month=profile.get('available_from_month') or profile.get('earliest_start',{}).get('month')
                low,high=_month_bounds(month)
            if target<low:reject.append('start before candidate availability')
            elif target<high:review.append('start overlaps unknown availability day')
            elif target==high and low!=high:review.append('start overlaps unknown availability day')
        except (ValueError,TypeError,AttributeError):review.append('start or candidate availability unknown/invalid')
    window=job.get('graduation_window')
    if window:
        try:
            lo,hi=_month_bounds(profile.get('graduation_month'))
            minimum=_month_bounds(window['min'])[0];maximum=_month_bounds(window['max'])[1]
            if maximum<minimum:raise ValueError('reversed window')
            if hi<minimum or lo>maximum:reject.append('graduation window incompatible')
            elif lo<minimum or hi>maximum:review.append('graduation window overlaps unknown completion day')
        except (ValueError,KeyError,TypeError):review.append('graduation month/window unknown or invalid')
    if re.search(r'\b(must|required|mandatory)\b.{0,80}\b(start|graduat)',job.get('description',''),re.I) and not (start or window):review.append('mandatory timing language needs grounded extraction')

def fit(profile,job):
    """Hard-constraint fixture interface. Full automatic eligibility also uses role_gate."""
    try:validate_skill_evidence(profile,job)
    except ValueError:return {'status':'review','reasons':['invalid skill evidence schema'],'reject_reasons':[],'review_reasons':['invalid skill evidence schema'],'matched_skills':[],'skill_gaps':[],'coverage':[0,1]}
    reject=[];review=[];title=norm(job.get('title',''));description=job.get('description','')
    if any(value is not None and type(value) is not bool for value in [job.get('us_citizenship_required'),profile.get('us_citizen')]):review.append('invalid citizenship evidence schema')
    if not title:review.append('missing title')
    if job.get('requirements_status')=='unreviewed':review.append('mandatory requirement extraction unverified')
    technical_staff=bool(re.search(r'\bmember of technical staff\b',title))
    level_title=re.sub(r'\bmember of technical staff\b','technical member',title)
    if re.search(r'\b(senior|sr|staff|principal|lead|manager|director|intern|internship|co op)\b',level_title):reject.append('excluded seniority or internship')
    if not re.search(r'\b(software|developer|swe|engineer)\b',title):
        if technical_staff:review.append('technical-staff role needs explicit software scope review')
        else:reject.append('not a software role')
    if re.search(r'\b(sales|support)\b|\b(mechanical|civil|manufacturing|hardware|electrical) engineer\b',title):reject.append('non-development role family')
    if norm(job.get('employment_type')) in {'intern','internship','parttime','part time','contract','temporary'}:reject.append('explicit non-fulltime or internship type')
    if any(job.get(k) is not None and type(job[k]) is not bool for k in ['active','is_listed']):review.append('invalid listing activity flags')
    if job.get('active') is False or job.get('is_listed') is False:reject.append('listing explicitly inactive')
    if job.get('degree_requirement') or re.search(r'\b(ph\.?d|masters?|bachelors?)\b.{0,60}\b(required|mandatory)\b',description,re.I):review.append('degree requirement needs grounded comparison')
    employer=norm(job.get('employer',''))
    if employer in {'booz allen','booz allen hamilton','northrop grumman','northrop grumman corporation','lockheed martin','lockheed martin corporation'}:review.append('contractor-associated employer; verify this role rather than infer from employer name')
    gov=job.get('government_contracting')
    # Remove only explicit negated commercial declarations, never an arbitrary nearby 'not'.
    contract_text=re.sub(r'\b(?:hold|perform|have|execute|undertake)\s+no\s+(?:government|federal)\s+contract(?:s|ing|or)?(?:\s+work)?\b','',description,flags=re.I)
    if gov is True or re.search(r'\b(active security clearance|security clearance required|federal contracts?|government contracts?|department of defense contractor|dod contractor|secret clearance|ts/sci)\b',contract_text,re.I):reject.append('government-contracting or clearance evidence')
    elif gov is not False:review.append('contracting classification unknown')
    location=job.get('location','');ln=norm(location)
    foreign=bool(re.search(r'\b(canada|india|united kingdom)\b',ln) or re.search(r'\b(on|ontario|bc|british columbia|qc|quebec) ca$',ln))
    mixed=any(x in location for x in [';',' / ',' | ']) or bool(re.search(r'\b(usa|united states)\b',ln))
    if foreign and not mixed:reject.append('explicit country outside US scope')
    elif not location_matches(location,profile.get('locations',[])):review.append('location unknown, mixed or outside approved map')
    need=profile.get('needs_sponsorship');sponsorship=job.get('sponsorship','unknown')
    if need is True:
        if sponsorship=='unavailable':reject.append('sponsorship incompatible')
        elif sponsorship!='available':review.append('sponsorship unknown')
    elif need is not False:review.append('candidate sponsorship facts unknown')
    citizenship_text=norm(description)
    citizenship_alternatives=bool(re.search(r'\bcitizens?(?:hip)? (?:or|and)\b|\bpermanent residents?\b',citizenship_text))
    citizenship_mandatory=bool(re.search(r'\b(must be (?:a )?(?:u s|us|united states) citizens?|(?:u s|us|united states) citizens? only|(?:u s|us|united states) citizens? are required|(?:u s|us|united states) citizenship (?:is )?(?:required|mandatory)|requires? (?:u s|us|united states) citizenship)\b',citizenship_text))
    if job.get('us_citizenship_required') is True or (citizenship_mandatory and not citizenship_alternatives):
        if profile.get('us_citizen') is False:reject.append('citizenship requirement incompatible')
        elif profile.get('us_citizen') is not True:review.append('citizenship requirement unknown; sponsorship is not citizenship evidence')
    elif re.search(r'\b(citizens?|citizenship|permanent residents?)\b',citizenship_text):
        review.append('citizenship/residency wording requires interpretation; no sponsorship inference')
    years=job.get('graduation_years') or [];year=profile.get('graduation_year')
    if years:
        if year is None:review.append('graduation year unknown')
        elif year not in years:reject.append('graduation window incompatible')
    _date_constraints(profile,job,reject,review)
    minimum=job.get('min_years')
    if minimum is not None:
        if type(minimum) not in (int,float) or not math.isfinite(minimum) or minimum<0:review.append('invalid experience requirement')
        elif minimum>2:reject.append('above proposed new-grad experience ceiling')
        elif minimum>0:
            exp=profile.get('experience_years')
            if exp is None:review.append('candidate experience unknown')
            elif exp<minimum:reject.append('experience incompatible')
    groups=job.get('required_skill_groups',[])
    skills={skill_key(s) for s in profile.get('skills',[])}
    required={skill_key(s) for s in job.get('required_skills',[])}
    matched=sorted(skills&required);gaps=sorted(required-skills)
    if gaps:review.append('mandatory skill evidence missing: '+', '.join(gaps))
    for group in groups:
        if not isinstance(group,list) or not group or not any(skill_key(x) in skills for x in group):review.append('mandatory alternative skill evidence missing')
    return {'status':'reject' if reject else 'review' if review else 'eligible','reasons':reject+review,'reject_reasons':reject,'review_reasons':review,'matched_skills':matched,'skill_gaps':gaps,'coverage':[len(matched),len(required) or 1],'skill_evidence':skill_evidence(profile,job)}

def role_gate(job,now):
    try:validate_skill_evidence(job=job)
    except ValueError:return ['invalid skill evidence schema']
    reasons=[];title=norm(job.get('title',''))
    if not re.search(r'\b(new grad|new graduate|early career|junior|associate|entry level)\b|software engineer i\b',title):reasons.append('early-career level not established')
    if job.get('employment_type')!='FullTime':reasons.append('full-time type not established')
    if job.get('is_listed') is False:reasons.append('unlisted')
    deadline=job.get('application_deadline')
    if deadline is not None:
        try:
            if instant(deadline)<=instant(now):reasons.append('deadline expired')
        except ValueError:reasons.append('deadline invalid')
    if job.get('mandatory_gaps'):reasons.append('mandatory qualification gaps')
    return reasons

def ashby_identity(url,employer=None):
    if not isinstance(url,str) or any(ord(c)<=32 or ord(c)==127 for c in url):raise ValueError('invalid raw Ashby URL')
    p=urlsplit(url);parts=p.path.strip('/').split('/')
    if p.scheme!='https' or p.hostname!='jobs.ashbyhq.com' or p.username is not None or p.password is not None or p.port not in (None,443) or p.fragment or not re.fullmatch(r'/[A-Za-z0-9_-]+/[0-9A-Fa-f-]+(?:/application)?/?',p.path):raise ValueError('untrusted Ashby identity')
    if employer is not None and parts[0].casefold()!=employer.casefold():raise ValueError('cross-employer Ashby identity')
    try:identifier=str(uuid.UUID(parts[1]))
    except ValueError:raise ValueError('invalid Ashby UUID') from None
    if identifier!=parts[1].lower():raise ValueError('noncanonical Ashby UUID')
    return parts[0].casefold(),identifier

def merge_key(job):
    u=job.get('url') or job.get('absolute_url')
    if u:
        p=urlsplit(u)
        if p.scheme!='https' or not p.netloc or p.username or p.password:raise ValueError('unsafe canonical URL')
        query=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in {'ref','source','oga'}]
        host=p.hostname.casefold();parts=p.path.strip('/').split('/')
        if host=='jobs.ashbyhq.com':return 'ashby:'+':'.join(ashby_identity(u))
        if host in {'boards.greenhouse.io','job-boards.greenhouse.io'} and len(parts)>=3 and parts[1]=='jobs' and parts[2].isdigit():return 'greenhouse:'+parts[0].lower()+':'+parts[2]
        return urlunsplit(('https',p.netloc.casefold(),p.path.rstrip('/'),urlencode(sorted(query)),''))
    return str(job['id'])

def source_record(kind,employer,raw):
    if not isinstance(raw,dict):raise ValueError('record must be object')
    if kind not in {'greenhouse','ashby'}:raise ValueError('unsupported source adapter')
    id=raw.get('id');title=raw.get('title');url=raw.get('absolute_url') if kind=='greenhouse' else raw.get('applyUrl')
    if kind=='ashby':
        board,id=ashby_identity(url or '',employer)
        employer=board
        if raw.get('jobUrl') and ashby_identity(raw['jobUrl'],employer)!=(board,id):raise ValueError('conflicting Ashby identities')
    if id is None or not isinstance(title,str) or not title.strip() or not url:raise ValueError('missing source identity/title/URL')
    description=text_only(raw.get('content','') if kind=='greenhouse' else raw.get('descriptionHtml',raw.get('descriptionPlain','')))
    location=raw.get('location');location=location.get('name','') if isinstance(location,dict) else location
    workplace='unknown'
    if raw.get('workplaceType') in ('Hybrid','Remote','OnSite'):workplace=raw['workplaceType'].casefold()
    j={'id':f'{kind}:{employer}:{id}','source_kind':kind,'requirements_status':'unreviewed','employer':employer,'title':title,'url':url,'location':location or '', 'description':description,'published_at':raw.get('first_published') if kind=='greenhouse' else raw.get('publishedAt'),'publication_semantics':'original' if kind=='greenhouse' and raw.get('first_published') else 'last_published' if kind=='ashby' else 'unknown','workplace':workplace,'employment_type':raw.get('employmentType','unknown'),'is_listed':raw.get('isListed') if kind=='ashby' else None,'application_deadline':raw.get('application_deadline'),'government_contracting':None,'sponsorship':'unknown','graduation_years':[],'required_skills':[],'min_years':None,'employer_notices':{k:raw.get(k) for k in ['ai_disclaimer','include_ai_disclaimer','ai_opt_out_request_url','data_compliance']}}
    merge_key(j)
    return j
