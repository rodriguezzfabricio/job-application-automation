"""Serve one armed account form over a user-private Unix socket for its funded worker."""
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time


def directory():
    return Path.home() / 'Library' / 'Application Support' / 'JobApplicationWorker' / 'portal'


def private_directory(folder, create=False):
    if create:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError('private worker socket directory required')


def request(page_url, folder=None):
    """Called by the native host. It has no access to the tracker or saved credentials."""
    folder = directory() if folder is None else folder
    try:
        private_directory(folder)
        name = (folder / 'active').read_text()
        if not re.fullmatch(r'[0-9a-f]{20}', name):
            return {'error': 'no_active_worker'}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(6)
            client.connect(str(folder / name))
            with client.makefile('rwb') as packet:
                packet.write(json.dumps({'page_url': page_url}).encode() + b'\n')
                packet.flush()
                reply = json.loads(packet.readline(16384))
                if reply.get('mode') in ('sign_in', 'register') and reply.get('email') and reply.get('password'):
                    return reply
                return {'error': 'no_matching_grant'}
    except (FileNotFoundError, ConnectionRefusedError):
        return {'error': 'no_active_worker'}
    except (OSError, ValueError, TypeError):
        return {'error': 'worker_unavailable'}


def start(store, grant, folder=None):
    from .store import Denied
    folder = directory() if folder is None else folder
    proc = subprocess.Popen([sys.executable, '-m', 'job_runtime.portal_bridge'],
                            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    try:
        proc.stdin.write(json.dumps({'database': str(store.path), 'folder': str(folder),
                                     'grant_id': grant['grant_id']}) + '\n')
        proc.stdin.close()
        if not select.select([proc.stdout], [], [], 7)[0] or proc.stdout.readline().strip() != 'ready':
            raise Denied('private autofill worker connection unavailable')
    except BaseException:
        proc.terminate()
        proc.wait(timeout=5)
        raise
    finally:
        proc.stdout.close()
    return proc


def serve(config):
    from . import account_flow
    from .store import Denied, Store
    grant_id = config['grant_id']
    folder = Path(config['folder'])
    private_directory(folder, create=True)
    endpoint = folder / grant_id[:20]
    store = Store(config['database'])
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            try:
                endpoint.chmod(0o600)
                server.listen(2)
                server.settimeout(3)
                with store.tx():
                    grant = account_flow.value(store, account_flow.KEY)
                    if grant.get('grant_id') != grant_id:
                        return
                    store.check(grant['owner'], grant['token'])
                    with tempfile.NamedTemporaryFile(mode='w', dir=folder, delete=False) as pointer:
                        pointer.write(endpoint.name)
                    os.replace(pointer.name, folder / 'active')
                print('ready', flush=True)
                sys.stdout.close()
                while time.time() < grant['expires']:
                    if account_flow.value(store, account_flow.KEY).get('grant_id') != grant_id:
                        break
                    try:
                        store.check(grant['owner'], grant['token'])
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    except Denied:
                        break
                    with client:
                        client.settimeout(3)
                        try:
                            with client.makefile('rwb') as packet:
                                url = json.loads(packet.readline(8193)).get('page_url')
                                if not isinstance(url, str) or len(url) > 8192:
                                    raise ValueError('invalid message')
                                try:
                                    reply = account_flow.consume(store, url, expected_grant=grant_id)
                                except Denied:
                                    reply = {'error': 'no_matching_grant'}
                                packet.write(json.dumps(reply).encode() + b'\n')
                                packet.flush()
                        except (ValueError, AttributeError, OSError):
                            continue
            finally:
                endpoint.unlink(missing_ok=True)
    finally:
        store.con.close()


if __name__ == '__main__':
    os.umask(0o077)
    serve(json.loads(sys.stdin.readline()))
