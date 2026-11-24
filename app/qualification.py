"""Evidence-based likely fit; source classification is fallible, not a guarantee.

Unlike the legacy inspection adapter, supported complete mandatory evidence can
qualify. Unknown classification/depth/eligibility never becomes a positive fact.
Authenticity, PDF and sending are separate release checks.
"""
import re
from matching import candidate_context
from policy import fit,role_gate
from requirement_review import inspect_fit
from semantic_segments import source_segments,CATEGORIES,is_future_duty

VERSION='qualified-evidence-7'
CUE=re.compile(r'\b(must|required|mandatory|minimum|experience|knowledge|proficien\w*|ability|understanding|degree|graduat\w*|authorized|sponsorship|citizenship|clearance|relocat\w*|government|federal|contract\w*|eligible|years?|residen\w*|on.site|hybrid|remote|visa|start|available)\b',re.I)
INJECTION=re.compile(r'ignore (?:all |previous |prior )*instructions|system prompt|send.{0,50}(?:secret|credential|resume)|override.{0,30}(?:policy|rules)',re.I)

def contracting_context(text):
    # Remove only this legal-jurisdiction phrase, not the EEO paragraph or any
    # neighboring role/contract restriction. "Federal contracts" stays visible.
    return re.sub(r'\bfederal\s*,\s*state\s*,?\s*(?:and|or)\s+local\s+laws?\b','legal jurisdiction',text,flags=re.I)

def qualify(profile,job,extraction,now,*,approval_sha256):
    context=candidate_context(profile,approval_sha256=approval_sha256)
    inspection=inspect_fit(profile,job,extraction,approval_sha256=approval_sha256)
    base=fit(context,dict(job,requirements_status='locally_checked'))
    # Absence of a government reference is a bounded negative observation, not
    # proof of citizenship or a fabricated employer declaration. Ambiguous
    # defense/public-sector context and contractor-associated employers stay review.
    contracting='explicit_source_classification' if job.get('government_contracting') is not None else 'unclear'
    no_contracting_reference=not re.search(r'\b(government|federal|defense|defence|clearance|public sector|military|dod|ts/sci|army|navy|air force|intelligence community|itar|export control|public trust|fedramp|govcloud|national security|nasa|dhs|disa|nsa|fbi|cia)\b',contracting_context(job['description']),re.I)
    if job.get('government_contracting') is None and no_contracting_reference:contracting='no_role_specific_evidence_observed'
    reasons=[r for r in base['reasons'] if not (r=='contracting classification unknown' and contracting=='no_role_specific_evidence_observed')]+role_gate(job,now)
    if not no_contracting_reference and job.get('government_contracting') is not True:reasons.append('government or defense context requires role-specific review')
    source={s['id']:s for s in source_segments(job['description'])}
    for row in extraction['classified_segments']:
        if row.get('category') not in CATEGORIES or row.get('importance') not in {'mandatory','preferred','unknown'}:raise ValueError('invalid classification schema')
        span=source[row['id']]
        if not span['is_heading'] and len(span['text'])<=100 and not re.search(r'[.!?;,]$',span['text']) and not (row['category']=='none' and is_future_duty(span)):reasons.append('unrecognized source heading or fragment requires review')
        if span['is_heading']:
            if row['category']!='none':reasons.append('heading has conflicting classification')
            continue
        if row.get('interpretation_warning'):reasons.append('source classification uncertainty')
        hard=bool(re.search(r'\b(must|required|mandatory|minimum)\b',row['quote'],re.I))
        if hard and row['importance']!='mandatory':reasons.append('explicit mandatory wording was not classified mandatory')
        if row['category']=='none' and (span['section'] in {'mandatory','preferred'} or CUE.search(contracting_context(row['quote']))):reasons.append('possible omitted eligibility condition')
        if INJECTION.search(row['quote']):reasons.append('instruction-like external content requires review')
    mandatory=[r for r in inspection['evidence_rows'] if r['importance']=='mandatory']
    uncertain=[r for r in inspection['evidence_rows'] if r['importance']=='unknown' or (r['importance']=='mandatory' and r['status']!='met')]
    if any(r['status']=='met' and not r['profile_evidence_ids'] for r in mandatory):reasons.append('mandatory match has no evidence reference')
    if not mandatory:reasons.append('no supported mandatory requirement inventory')
    if uncertain:reasons.append('mandatory or ambiguous evidence needs review')
    reject=bool(base['reject_reasons']) or inspection['status']=='reject'
    status='reject' if reject else 'review' if reasons else 'eligible'
    reasons += [r for r in inspection['reasons'] if r.startswith(('Confirmed requirement not met:','Possible requirement mismatch needs review:','Preferred requirement not met:'))]
    return {'policy_version':VERSION,'status':status,'label':'likely_fit' if status=='eligible' else status,'reasons':list(dict.fromkeys(reasons)),
            'evidence_rows':inspection['evidence_rows'],'mandatory_met':sum(r['status']=='met' for r in mandatory),'contracting_assessment':contracting,
            'requirements_verified':status=='eligible','application_ready':False,'profile_hash':approval_sha256,
            'description_sha256':inspection['description_sha256'],'extraction_version':inspection['extraction_version'],
            'limits':'Likely fit under complete source classifications and supported predicates; classification can err. This is not employer screening approval or source/document/delivery validation.'}
