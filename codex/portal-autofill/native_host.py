#!/usr/bin/env python3
"""Brave Native Messaging host. Never print a credential to a terminal or a log."""
import json
import re
import os
from pathlib import Path
import struct
import sys
import tempfile
import time

def allowed_origin():
    manifest = Path.home() / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/local.jobs.portal_autofill.json'
    try:
        config = json.loads(manifest.read_text())
        origins = config.get('allowed_origins', [])
        if (config.get('path') == str(Path(__file__).resolve()) and len(origins) == 1
                and re.fullmatch(r'chrome-extension://[a-p]{32}/', origins[0])):
            return origins[0]
    except (OSError, ValueError, TypeError):
        pass
    return None


def status(kind,stage,outcome):
    """Only fixed diagnostics. Never log the native message, URL or credential reply."""
    folder=Path.home()/'Library/Logs/JobApplicationWorker'
    try:
        folder.mkdir(parents=True,mode=0o700,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',dir=folder,delete=False) as f:
            json.dump({'at':time.time(),'request':kind,'stage':stage,'outcome':outcome},f)
            filename=f.name
        os.replace(filename,folder/('portal-autofill-'+kind+'.json'))
    except OSError:
        pass

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] != allowed_origin() or sys.stdout.isatty():
        raise SystemExit(2)
    result = {'error': 'private autofill unavailable'}
    kind,stage,outcome='fill','message','invalid_request'
    try:
        length, = struct.unpack('=I', sys.stdin.buffer.read(4))
        if not 0 < length <= 8192:
            raise ValueError()
        data = json.loads(sys.stdin.buffer.read(length))
        kind='health' if data=={'op':'health'} else 'fill'
        stage='runtime_import'
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        if data == {'op':'health'}:
            result = {'ready':True,'protocol':1}
            stage,outcome='native_host','ready'
        else:
            from job_runtime import portal_bridge
            stage='worker_connection'
            if set(data) != {'page_url'} or not isinstance(data['page_url'],str):
                raise ValueError()
            result = portal_bridge.request(data['page_url'])
            outcome=result.get('error','grant_consumed')
    except (ValueError,KeyError,TypeError,OSError,ImportError,struct.error):
        outcome='invalid_request' if stage=='message' else 'runtime_error'
    status(kind,stage,outcome)
    message = json.dumps(result).encode()
    sys.stdout.buffer.write(struct.pack('=I', len(message)) + message)
    sys.stdout.buffer.flush()
