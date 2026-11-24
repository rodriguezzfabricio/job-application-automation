#!/usr/bin/env python3
"""Queue a deduplicated local notification for the configured recipient."""
import datetime, hashlib, json, os, re, sys

BOX = os.path.expanduser("~/Library/Application Support/job-watchdog")
OUT = os.path.join(BOX, "outbox")
SENT_KEYS = os.path.join(BOX, "sent-keys.json")


def main(argv):
    args = argv[1:]
    if not args or args[0].startswith("--"):
        print(__doc__)
        return 2
    msg = " ".join(args[0].split())[:600]
    key = args[args.index("--key") + 1] if "--key" in args else msg
    hours = float(args[args.index("--hours") + 1]) if "--hours" in args else 12.0
    now = datetime.datetime.now()
    os.makedirs(OUT, exist_ok=True)
    try:
        sent = json.load(open(SENT_KEYS))
    except Exception:
        sent = {}
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    last = sent.get(h)
    if last and (now - datetime.datetime.fromisoformat(last)).total_seconds() < hours * 3600:
        print(json.dumps({"queued": False, "why": f"same alert sent at {last[:16]}"}))
        return 0
    sent[h] = now.isoformat(timespec="seconds")
    cutoff = now - datetime.timedelta(days=3)
    sent = {k: v for k, v in sent.items() if datetime.datetime.fromisoformat(v) > cutoff}
    json.dump(sent, open(SENT_KEYS, "w"))
    name = now.strftime("%Y%m%d-%H%M%S-") + h[:6] + ".txt"
    tmp = os.path.join(BOX, "." + name)
    open(tmp, "w", encoding="utf-8").write(re.sub(r"[\r\n]+", " ", msg))
    os.replace(tmp, os.path.join(OUT, name))  # appears whole, so the sender never reads half a file
    print(json.dumps({"queued": True, "file": os.path.join(OUT, name)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
