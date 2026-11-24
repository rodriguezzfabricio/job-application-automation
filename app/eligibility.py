"""Whole-clause eligibility predicates grounded only in caller-verified facts.

Month bounds are internal comparison intervals, not candidate dates. Unparsed
wording, incomplete facts, compounds, seasons and equivalence remain unknown.
This module neither verifies candidate provenance nor certifies entire jobs;
requirement_review supplies the approved-profile/source boundary.
"""
import calendar,math,re
from datetime import date

VERSION='explicit-eligibility-7'
ENGLISH_MONTHS=('January','February','March','April','May','June','July','August','September','October','November','December')
MONTHS={alias.casefold():n for n,name in enumerate(ENGLISH_MONTHS,1) for alias in (name,name[:3])}
MONTHS['sept']=9
MONTH_PATTERN='(?:'+'|'.join(sorted(MONTHS,key=len,reverse=True))+r')\.?'
BOUND=r'(?:'+MONTH_PATTERN+r' (?:\d{1,2},? )?\d{4}|\d{4}-\d{2}-\d{2}|\d{4}-\d{2})'


def _month(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}',value):raise ValueError('confirmed ISO month required')
    year,month=map(int,value.split('-'))
    return date(year,month,1),date(year,month,calendar.monthrange(year,month)[1])


def _bound(value):
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        day=date.fromisoformat(value);return day,day
    if re.fullmatch(r'\d{4}-\d{2}',value):return _month(value)
    match=re.fullmatch(r'([A-Za-z]+)\.? (?:(\d{1,2}),? )?(\d{4})',value)
    if not match:raise ValueError('unsupported bound')
    name,day,year=match.groups();month=MONTHS[name.casefold()];year=int(year)
    if day:
        result=date(year,month,int(day));return result,result
    return date(year,month,1),date(year,month,calendar.monthrange(year,month)[1])


def _inside(candidate,allowed):
    lo,hi=candidate;minimum,maximum=allowed
    if maximum<minimum:raise ValueError('reversed allowed window')
    if hi<minimum or lo>maximum:return 'not_met'
    if lo>=minimum and hi<=maximum:return 'met'
    return 'unknown'


def _graduation(profile):
    education=profile.get('education',{})
    if education.get('enrollment_status') in {'withdrawn','not_enrolled','inactive'} and education.get('degree_completed') is not True:raise ValueError('graduation needs reconfirmation after enrollment change')
    nested=education.get('graduation_month');top=profile.get('graduation_month')
    if nested is not None and top is not None and nested!=top:raise ValueError('conflicting graduation month')
    return _month(nested if nested is not None else top)


def _graduation_ref(profile):
    return 'profile:education.graduation_month' if profile.get('education',{}).get('graduation_month') is not None else 'profile:graduation_month'


def _availability(profile):
    start=profile.get('earliest_start')
    if not isinstance(start,dict) or start.get('precision')!='month' or start.get('day') is not None:raise ValueError('confirmed month availability required')
    return _month(start.get('month'))


def _degree(value):
    if not isinstance(value,str):return None
    value=re.sub(r'[^a-z0-9]+',' ',value.casefold()).strip()
    if re.fullmatch(r'(?:bs|b s|bsc|bachelor of science)(?: in)? computer science',value):return ('bachelor','computer science')
    if re.fullmatch(r'(?:ms|m s|msc|master of science)(?: in)? computer science',value):return ('master','computer science')
    return None


def _combined_cohort(profile,text):
    pattern=(r'To be eligible for this role, you need to graduate (in|by) ('+MONTH_PATTERN+r' \d{4}) and be able to start (?:full time|FTE) by ('+MONTH_PATTERN+r')(?:/('+MONTH_PATTERN+r'))? (\d{4})[.]?')
    match=re.fullmatch(pattern,text,re.I)
    if not match:return None
    mode,graduation,a,b,year=match.groups()
    if b and MONTHS[b.rstrip('.').casefold()]<=MONTHS[a.rstrip('.').casefold()]:return 'unknown',[],'Slash deadline order is ambiguous; no employer year boundary is inferred.'
    allowed=_bound(graduation);deadlines=[_bound(name+' '+year) for name in (a,b) if name]
    refs=[]
    try:
        candidate=_graduation(profile)
        gradstate=_inside(candidate,allowed) if mode.casefold()=='in' else 'met' if candidate[1]<=allowed[1] else 'not_met' if candidate[0]>allowed[1] else 'unknown'
        refs.append(_graduation_ref(profile))
    except (ValueError,TypeError,KeyError,AttributeError):gradstate='unknown'
    try:
        lo,hi=_availability(profile)
        states=['met' if hi<=minimum else 'not_met' if lo>maximum else 'unknown' for minimum,maximum in deadlines]
        startstate='met' if all(x=='met' for x in states) else 'not_met' if all(x=='not_met' for x in states) else 'unknown'
        refs.append('profile:earliest_start')
    except (ValueError,TypeError,KeyError,AttributeError):startstate='unknown'
    status='not_met' if 'not_met' in (gradstate,startstate) else 'met' if (gradstate,startstate)==('met','met') else 'unknown'
    return status,refs,'Graduation month: '+gradstate+'; full-time availability: '+startstate+'. Each possible stated start-deadline month is checked; no candidate day or employer season is invented.'


def evaluate_clause(profile,quote):
    result={'status':'unknown','source_quote':quote,'predicate':None,'profile_evidence_ids':[],'explanation':'This wording or its required facts need review; no unsupported assumption is made.'}
    if not isinstance(profile,dict) or not isinstance(quote,str) or not quote.strip() or len(quote)>10000:return result
    text=' '.join(quote.split())
    def answer(status,predicate,refs,explanation):
        return dict(result,status=status,predicate=predicate,profile_evidence_ids=refs,explanation=explanation)
    try:
        if re.fullmatch(r'(?:No (?:visa )?sponsorship is (?:offered|available)|(?:Visa )?sponsorship is not (?:offered|available))[.!]?',text,re.I):
            need=profile.get('needs_sponsorship')
            if type(need) is bool:return answer('not_met' if need else 'met','sponsorship_unavailable',['profile:needs_sponsorship'],'Uses only the explicitly confirmed sponsorship need; citizenship, clearance and other authorization requirements remain separate.')
            return answer('unknown','sponsorship_unavailable',[],'Sponsorship need is unconfirmed; no citizenship or authorization inference is made.')
        combined=_combined_cohort(profile,text)
        if combined:return answer(combined[0],'combined_graduation_start',combined[1],combined[2])
        pursuing=re.fullmatch(r"Pursuing a bachelor['’]s or master['’]s degree in computer science, engineering, or another related field[.]?",text,re.I)
        if pursuing:
            education=profile.get('education',{})
            supported=_degree(education.get('degree')) in {('bachelor','computer science'),('master','computer science')}
            enrolled=education.get('enrollment_status') in {'in_progress','pursuing','enrolled'}
            if supported and enrolled and (education.get('degree_completed') is None or education.get('degree_completed') is False):return answer('met','pursuing_degree',['profile:education.degree','profile:education.enrollment_status'],'Explicitly confirmed current enrollment in the named CS degree supports pursuing; expected graduation alone is not enrollment or completion.')
            return answer('unknown','pursuing_degree',[],'Current enrollment and a directly named degree need explicit confirmation; no related-field equivalence or enrollment from a future date is inferred.')
        if re.fullmatch(r'Previous internship experience[.]?',text,re.I):
            count=profile.get('internships_count')
            if type(count) is int and count>=0:return answer('met' if count>=1 else 'not_met','internship_count',['profile:internships_count'],'Confirmed previous internship count; duration and requirement importance are not inferred.')
            return answer('unknown','internship_count',[],'Previous internship count is missing or invalid.')
        # Entire explicit clause, not a convenient substring of a longer demand.
        window=re.fullmatch(r'(?:Must graduate between|Graduation between) ('+BOUND+r') and ('+BOUND+r')(?: required)?[.]?',text,re.I)
        if window:
            a,b=window.groups();allowed=(_bound(a)[0],_bound(b)[1]);state=_inside(_graduation(profile),allowed)
            return answer(state,'graduation_window',[_graduation_ref(profile)],'Compares the confirmed graduation month with the explicit employer window; no graduation day is invented.')
        cohort=re.fullmatch(r'Must graduate in (\d{4}(?: or \d{4})*)[.]?',text,re.I)
        if cohort:
            years={int(y) for y in re.split(r'\s+or\s+',cohort[1],flags=re.I)};lo,hi=_graduation(profile)
            if any(not 1<=year<=9999 for year in years):raise ValueError('invalid cohort year')
            return answer('met' if lo.year in years else 'not_met','graduation_cohort',[_graduation_ref(profile)],'The confirmed graduation month belongs to an explicitly accepted year cohort.')
        start=re.fullmatch(r'Must start (on|by|in) ('+BOUND+r')[.]?',text,re.I)
        if start:
            mode,value=start.groups();mode=mode.casefold();target_lo,target_hi=_bound(value);low,high=_availability(profile)
            if mode=='on' and target_lo!=target_hi:raise ValueError('on requires exact day')
            if mode=='in' and target_lo==target_hi:raise ValueError('in requires month')
            deadline=target_lo
            status='met' if high<=deadline else 'not_met' if low>target_hi else 'unknown'
            return answer(status,'start_availability',['profile:earliest_start'],'Compares the stated start requirement with confirmed availability; graduation alone is not availability.')
        degree=re.fullmatch(r"(Bachelor|Master)'s degree in (Computer Science)(?: required|, graduating between ("+BOUND+r') and ('+BOUND+r'))[.]?',text,re.I)
        if degree:
            level,field,a,b=degree.groups();required=(level.casefold(),field.casefold());education=profile.get('education',{});actual=_degree(education.get('degree'))
            if a and b:
                window_state=_inside(_graduation(profile),(_bound(a)[0],_bound(b)[1]))
                if window_state=='not_met':return answer('not_met','graduation_window',[_graduation_ref(profile)],'The confirmed graduation month is outside the mandatory cohort even though the degree requirement may need review.')
            if actual!=required:return answer('unknown','degree',['profile:education.degree'],'The verified degree does not establish this exact requirement; equivalence or additional degrees are not assumed.')
            if a and b:
                state=_inside(_graduation(profile),(_bound(a)[0],_bound(b)[1]))
                return answer(state,'planned_degree_and_cohort',['profile:education.degree',_graduation_ref(profile)],'The approved planned degree and graduation month match the explicit future-graduate cohort; this does not claim the degree is already earned.')
            if education.get('degree_completed') is True:return answer('met','completed_degree',['profile:education.degree','profile:education.degree_completed'],'Uses explicit verified degree completion, not an expected graduation date.')
            return answer('unknown','completed_degree',['profile:education.degree'],'The planned degree is relevant, but completion has not been confirmed for this requirement.')
        internship=re.fullmatch(r'At least (\d+) internships? required[.]?',text,re.I)
        if internship:
            minimum=int(internship[1]);count=profile.get('internships_count')
            if type(count) is not int or count<0:return result
            return answer('met' if count>=minimum else 'not_met','internship_count',['profile:internships_count'],'Compares confirmed internship count only; no employment duration is inferred.')
        experience=re.fullmatch(r'(?:At least )?(\d+(?:\.\d+)?)(?:\+)? years? of (?:(commercial full[- ]time|full[- ]time) )?experience required[.]?',text,re.I)
        if experience:
            minimum=float(experience[1])
            if not math.isfinite(minimum):return result
            kind=(experience[2] or '').casefold().replace('-',' ')
            field='commercial_fulltime_experience_years' if kind=='commercial full time' else 'fulltime_experience_years' if kind=='full time' else 'experience_years'
            amount=profile.get(field)
            if type(amount) not in (int,float) or not math.isfinite(amount) or amount<0:return result
            return answer('met' if amount>=minimum else 'not_met','experience_years',['profile:'+field],'Compares explicitly verified years of the same experience type; internship counts and other experience types are not substituted.')
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):return dict(result,predicate='invalid_clause_or_profile',explanation='Dates, precision, bounds or profile facts are missing, invalid or conflicting; review required.')
    if re.search(r'\b(winter|spring|summer|fall|autumn)\b',text,re.I):result['explanation']='Employer season boundaries are undefined; no season-to-month inference is made.'
    return result
