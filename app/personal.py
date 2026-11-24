"""Source-bound personal PDF drafts. No mail or network access."""
import io,json,re,hashlib,os,stat
from pathlib import Path
from xml.sax.saxutils import escape
from pypdf import PdfReader
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import letter
from packet import _font_setup,compact,canonical,digest
from reportlab.pdfgen.canvas import Canvas
from policy import skill_labels,skill_mentioned,validate_skill_evidence

def validate_evidence(profile,*,approval_sha256=None):
    validate_skill_evidence(profile=profile)
    if not isinstance(approval_sha256,str) or digest(canonical(profile))!=approval_sha256:raise ValueError('external approved profile digest required')
    if not profile.get('source'):raise ValueError('original source PDF required')
    if any(g.get('section') not in {'experience','projects'} for g in profile.get('groups',{}).values()):raise ValueError('unsupported section')
    if profile.get('synthetic') is not False:raise ValueError('explicit personal profile required')
    bank=profile.get('variants',[])
    if not skill_labels(profile.get('skills',[])) or any(not skill_labels(v.get('skills',[])) for v in bank):raise ValueError('invalid skill evidence schema')
    if not bank or len({v['id'] for v in bank})!=len(bank):raise ValueError('unique evidence required')
    for v in bank:
        if not isinstance(v.get('text'),str) or not compact(v['text']):raise ValueError('nonempty original-source claim required')
        facts=v.get('facts',[])
        if len(facts)!=1 or facts[0] not in profile.get('facts',{}):raise ValueError('single original-source fact required')
        fact=profile['facts'][facts[0]]
        if v['group'] not in profile['groups'] or fact['group']!=v['group'] or v['text']!=fact['claim_text']:raise ValueError('variant does not exactly match approved source fact')
    if 'source' in profile:
        source=profile['source'];path=Path(source['path'])
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source_file:
            info=os.fstat(source_file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>20*1024*1024:raise ValueError('bounded regular source PDF required')
            raw=source_file.read(20*1024*1024+1)
        if len(raw)>20*1024*1024:raise ValueError('source PDF too large')
        if hashlib.sha256(raw).hexdigest()!=source['sha256']:raise ValueError('source PDF changed')
        actual=compact(' '.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(raw)).pages))
        for v in bank:
            if compact(v['text']) not in actual:raise ValueError('claim not present in original PDF')
    return bank

def select_variants(profile,job,max_bullets=8,*,approval_sha256=None):
    validate_skill_evidence(profile,job)
    bank=validate_evidence(profile,approval_sha256=approval_sha256)
    if type(max_bullets) is not int or not 1<=max_bullets<=14:raise ValueError('invalid bullet budget')
    desc=(job.get('title','')+' '+job.get('description','')).casefold()
    def score(v):return sum(skill_mentioned(s,desc) for s in v.get('skills',[]))
    ranked=sorted(enumerate(bank),key=lambda pair:(-score(pair[1]),pair[0]))
    # Job-specific evidence order with source order as a deterministic tie-break.
    return [v['id'] for _,v in ranked[:max_bullets]]

def render_resume(profile,job,selected,*,approval_sha256=None,job_binding=None):
    validate_skill_evidence(profile,job)
    bank=validate_evidence(profile,approval_sha256=approval_sha256);byid={v['id']:v for v in bank}
    if not selected or len(set(selected))!=len(selected) or any(x not in byid for x in selected):raise ValueError('invalid selected evidence')
    chosen=[byid[x] for x in selected];glyphs=_font_setup()
    body=ParagraphStyle('personal-body',fontName='SampleVera',fontSize=10.5,leading=12.5,spaceAfter=4)
    head=ParagraphStyle('personal-heading',parent=body,fontName='SampleVeraBold',fontSize=10.5,spaceBefore=7,spaceAfter=4)
    name=ParagraphStyle('personal-name',parent=head,fontSize=17,leading=20,spaceBefore=0,spaceAfter=5)
    strings=[];story=[]
    def add(s,style=body):
        s=str(s)
        if len(s)>3000 or any(ord(c) not in glyphs for c in s if not c.isspace()):raise ValueError('unsupported or oversized content')
        strings.append(s);story.append(Paragraph(escape(s),style))
    add(profile['name'],name)
    add(' | '.join(x for x in [profile.get('phone'),profile['email']] if x))
    if profile.get('links'):add(' | '.join(profile['links']))
    add('EDUCATION',head);e=profile['education'];add(e['institution']+' | '+e['degree']+' | '+e['graduation_month'])
    add('TECHNICAL SKILLS',head)
    desc=job.get('description','').casefold()
    skills=sorted(enumerate(profile.get('skills',[])),key=lambda s:(not skill_mentioned(s[1],desc),s[0]))
    source_text=' '.join(page.extract_text() or '' for page in PdfReader(profile['source']['path']).pages)
    if any(not skill_mentioned(s,source_text) for _,s in skills[:18]):raise ValueError('rendered skill lacks original PDF support')
    add(', '.join(s for _,s in skills[:18]))
    for section,label in [('experience','EXPERIENCE'),('projects','PROJECTS')]:
        groups=[(gid,g) for gid,g in profile['groups'].items() if g.get('section')==section and any(v['group']==gid for v in chosen)]
        if not groups:continue
        add(label,head)
        for gid,g in groups:
            add(g['name']+' | '+g.get('dates',''),head)
            if g.get('role'):add(g['role']+(' | '+g['location'] if g.get('location') else ''))
            for v in chosen:
                if v['group']==gid:add('- '+v['text'])
    if job_binding is not None and (not isinstance(job_binding,str) or not re.fullmatch('[a-f0-9]{64}',job_binding)):raise ValueError('invalid document job binding')
    out=io.BytesIO();doc=SimpleDocTemplate(out,pagesize=letter,leftMargin=36,rightMargin=36,topMargin=30,bottomMargin=30,title='Tailored resume',author=profile['name'],subject='Job fingerprint: '+job_binding if job_binding else 'Professional resume')
    def invariant_canvas(*args,**kwargs):
        kwargs['invariant']=1;return Canvas(*args,**kwargs)
    doc.build(story,canvasmaker=invariant_canvas);pdf=out.getvalue();r=PdfReader(io.BytesIO(pdf))
    if len(r.pages)!=1 or len(pdf)>1024*1024:raise ValueError('one-page draft overflow; revise selection without shrinking text')
    actual=compact(r.pages[0].extract_text() or '')
    if actual!=compact(' '.join(strings)):raise ValueError('exact PDF text validation failed')
    return pdf
