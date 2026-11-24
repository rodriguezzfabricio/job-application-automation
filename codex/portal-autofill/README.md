# Optional local account autofill

Load this folder as an unpacked extension in the intended Brave profile. Copy its displayed extension ID. Run `python3 setup.py --register --extension-id YOUR_EXTENSION_ID` from this directory to register the native messaging host, then `python3 setup.py --probe` to check registration. The extension accepts a single armed request for the verified employer account origin. Private credentials stay in the operator's local configuration.
