"""Bind a personal profile to existing policy and expose draft source requirements.

The inventory is inspection data, not a verified semantic extractor. Real source
records retain requirements_status=unreviewed and cannot qualify from keywords.
"""
import copy
import re
from personal import validate_evidence
from policy import fit, role_gate, _month_bounds

_REQUIRED={"skills you'll need to bring",'required skills','must haves','must-haves','requirements','qualifications','basic qualifications','minimum qualifications','required qualifications','what you have','what you bring',"what you'll bring",'what we look for',"what we're looking for",'who you are'}
_PREFERRED={'nice to haves','nice-to-haves','preferred experience','preferred requirements','preferred','preferred qualifications','preferred skills','nice to have','bonus','bonus points'}
_OTHER={'responsibilities',"what you'll do",'what you will do',"what you'll achieve",'who we are','compensation','benefits','about us','about the role','role overview','why join us'}


def candidate_context(profile, *, approval_sha256=None):
    validate_evidence(profile,approval_sha256=approval_sha256)
    result=copy.deepcopy(profile)
    pending=profile.get('unverified_fields',[])
    allowed={'earliest_start','experience_years','fulltime_experience_years','commercial_fulltime_experience_years','internships_count','us_citizen','work_authorized_us','needs_sponsorship','willing_to_relocate'}
    if not isinstance(pending,list) or any(not isinstance(field,str) or field not in allowed for field in pending):raise ValueError('invalid uncertainty metadata')
    for field in pending:result.pop(field,None)
    if 'earliest_start' in pending:
        for field in ['available_from_month','availability_month','available_from_date']:result.pop(field,None)
    month=profile.get('education',{}).get('graduation_month')
    if month is not None:
        _month_bounds(month)
        if profile.get('graduation_month') not in (None,month):raise ValueError('conflicting graduation month')
        if any(year not in (None,int(month[:4])) for year in [profile.get('graduation_year'),profile['education'].get('graduation_year')]):raise ValueError('conflicting graduation facts')
        result['graduation_month']=month;result['graduation_year']=int(month[:4])
    start=result.get('earliest_start')
    if start is not None:
        if not isinstance(start,dict) or start.get('precision')!='month' or start.get('day') is not None:raise ValueError('only confirmed month-precision availability supported')
        _month_bounds(start.get('month'))
        if any(result.get(field) not in (None,start['month']) for field in ['available_from_month','availability_month']):raise ValueError('conflicting availability month')
        result['availability_month']=start['month']
        # Never derive a day from a month or let conflicting old fields win.
        if result.get('available_from_date'):raise ValueError('conflicting availability precision')
        result.pop('available_from_date',None)
    if 'willing_to_relocate' in result and type(result['willing_to_relocate']) is not bool:raise ValueError('relocation confirmation must be boolean')
    return result


def requirement_inventory(description):
    if not isinstance(description,str) or not description.strip() or len(description)>500000:raise ValueError('bounded nonempty description required')
    result=[];section=None;offset=0
    for raw in description.splitlines(keepends=True):
        text=raw.strip();heading=text.rstrip(':').strip().casefold().replace('\u2019',"'")
        if heading in _REQUIRED:section='mandatory'
        elif heading in _PREFERRED:section='preferred'
        elif heading in _OTHER:section=None
        elif text:
            required=bool(re.search(r'\b(must|required|minimum|at least|eligible|eligibility)\b',text,re.I))
            preferred=bool(re.search(r'\b(preferred|nice.to.have|is a plus)\b',text,re.I))
            uncertain=bool(re.search(r'\b(ideally|possibly|maybe)\b|\b(?:no|not|without)\b.{0,90}\b(?:required|mandatory|minimum)\b',text,re.I))
            conflict=(required and preferred) or (section=='mandatory' and preferred) or (section=='preferred' and required)
            importance='unknown' if uncertain or conflict else 'mandatory' if required else 'preferred' if preferred else section or 'unknown'
            if importance:
                start=offset+len(raw)-len(raw.lstrip());end=start+len(text)
                result.append({'id':'span-'+str(start),'start':start,'end':end,'quote':description[start:end],'importance':importance,'extraction_status':'draft','semantic_requirement_verified':False})
        offset+=len(raw)
    return result


def assess(profile,job,now,*,approval_sha256=None):
    context=candidate_context(profile,approval_sha256=approval_sha256)
    # This adapter inventories text; it does not certify extraction. A field in
    # source content or a raw caller dictionary cannot manufacture that trust.
    inspection_job=dict(job,requirements_status='unreviewed')
    decision=fit(context,inspection_job);gate=role_gate(inspection_job,now)
    result=copy.deepcopy(decision)
    result['reasons']=list(dict.fromkeys(decision['reasons']+gate))
    if gate and result['status']!='reject':result['status']='review'
    if result['status']!='reject':result['status']='review'
    result['reasons']=list(dict.fromkeys(result['reasons']+['mandatory requirement extraction unverified']))
    result['requirements_verified']=False
    result['requirement_inventory']=requirement_inventory(job.get('description',''))
    result['profile_evidence']={'graduation_month':context.get('graduation_month'),'availability_month':context.get('availability_month'),'willing_to_relocate':context.get('willing_to_relocate'),'citizenship_known':context.get('us_citizen') is True or context.get('us_citizen') is False}
    result['application_ready']=False  # Qualification alone never certifies source/files/delivery.
    return result
