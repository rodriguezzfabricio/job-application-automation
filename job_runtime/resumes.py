"""Recoverable unchanged-PDF publication. Manifest switches last; journal recovers interruption."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from .backup import file_hash

def recover(library):
    library=Path(library);journal=library.parent/'resume-publish-journal.json'
    if not journal.exists(): return False
    state=json.loads(journal.read_text());backup=Path(state['backup'])
    # Restore prior bytes before another worker may use the library.
    for p in backup.iterdir():
        if p.is_file():
            temp=library/('.restore-'+uuid.uuid4().hex);shutil.copy2(p,temp);os.replace(temp,library/p.name)
    journal.replace(journal.with_name('resume-publish-recovered-'+uuid.uuid4().hex+'.json'))
    return True

def publish(source,library,manifest,validate):
    source=Path(source);library=Path(library);library.mkdir(parents=True,exist_ok=True)
    recover(library)
    with tempfile.TemporaryDirectory(dir=library.parent,prefix='resume-stage-') as tmp:
        staged=Path(tmp)
        for entry in manifest['resumes']:
            p=source/entry['file'];shutil.copy2(p,staged/p.name)
            if file_hash(staged/p.name)!=entry['sha256']: raise ValueError('source changed during resume staging')
        (staged/'manifest.json').write_text(json.dumps(manifest,indent=1));validate(str(staged))
        backup=library/('_retired-publish-'+uuid.uuid4().hex);backup.mkdir()
        for p in library.iterdir():
            if p.is_file() and (p.suffix.lower()=='.pdf' or p.name=='manifest.json'):shutil.copy2(p,backup/p.name)
        journal=library.parent/'resume-publish-journal.json'
        with journal.open('w') as f:
            json.dump({'backup':str(backup),'phase':'publishing'},f);f.flush();os.fsync(f.fileno())
        try:
            for entry in manifest['resumes']:
                dest=library/entry['file']
                os.replace(staged/entry['file'],dest);dest.chmod(0o444)
            os.replace(staged/'manifest.json',library/'manifest.json')
            journal.replace(journal.with_name('resume-publish-complete-'+uuid.uuid4().hex+'.json'))
        except BaseException:
            recover(library);raise
    return str(backup)


def verify_attachment(private_root, job, selected_name, reviewed_hash, evidence):
    """Bind the reviewed local PDF to the file observed on this opening."""
    from .store import Denied
    if not isinstance(evidence, dict) or evidence.get('job_key') != job['job_key'] or not evidence.get('refs'):
        raise Denied('attachment readback must identify this opening')
    path = Path(private_root)/'my-resumes'/selected_name
    if evidence.get('observed_name') != selected_name or type(evidence.get('observed_bytes')) is not int or evidence['observed_bytes'] != path.stat().st_size:
        raise Denied('visible attachment name or size differs from the selected PDF')
    if evidence.get('method') == 'browser_file':
        if evidence.get('observed_sha256') != reviewed_hash:
            raise Denied('observed attachment bytes differ from the selected PDF')
    elif evidence.get('method') == 'filechooser':
        if evidence.get('selected_path') != str(path.resolve()) or evidence.get('selected_sha256') != reviewed_hash:
            raise Denied('file chooser must select the verified local PDF')
    else:
        raise Denied('browser file or file chooser readback required')
