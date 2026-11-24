"""Check or register the local Brave Native Messaging host without changing browser settings."""
import argparse
import json
import re
import struct
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--register', action='store_true')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--extension-id', help='ID shown for the unpacked extension in Brave')
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    name = 'local.jobs.portal_autofill.json'
    # On macOS Brave overrides DIR_USER_NATIVE_MESSAGING to Chrome's per-user location.
    target = Path.home() / 'Library/Application Support/Google/Chrome/NativeMessagingHosts' / name
    config = json.loads((folder / name).read_text())
    config['path'] = str(folder / 'native_host.py')
    if args.register:
        if not re.fullmatch('[a-p]{32}', args.extension_id or ''):
            parser.error('--register requires the exact --extension-id shown in Brave')
        config['allowed_origins'] = ['chrome-extension://' + args.extension_id + '/']
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('x') as out:
                json.dump(config, out, indent=2)
            target.chmod(0o600)
    installed = json.loads(target.read_text()) if target.is_file() else {}
    origins = installed.get('allowed_origins', [])
    if not args.register and len(origins) == 1 and re.fullmatch(r'chrome-extension://[a-p]{32}/', origins[0]):
        config['allowed_origins'] = origins
    ready = bool(config['allowed_origins']) and installed == config
    probe = None
    if args.probe and ready:
        data = b'{"op":"health"}'
        proc = subprocess.run([config['path'],config['allowed_origins'][0]],input=struct.pack('=I',len(data))+data,
                              capture_output=True,timeout=8,env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin'})
        if proc.returncode==0 and len(proc.stdout)>=4:
            size,=struct.unpack('=I',proc.stdout[:4])
            probe=(size==len(proc.stdout)-4 and json.loads(proc.stdout[4:]).get('ready') is True)
        else: probe=False
    print(json.dumps({'native_host_registered':ready,'private_host_probe':probe,'extension_folder':str(folder),
                      'browser_setup':'Load the unpacked extension in the intended Brave profile when enabling private autofill.'}))


if __name__ == '__main__':
    main()
