"""Optionally check installed prompt routing rules."""
import glob, json, os, shutil, subprocess, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUDGET_JS = os.environ.get('JOB_ROUTING_HOOK', '')
FILES = sorted(glob.glob(os.path.join(ROOT, 'codex', 'stages', '*.md')))
if os.environ.get('JOB_PROMPT_DIR'):
    FILES.extend(sorted(glob.glob(os.path.join(os.environ['JOB_PROMPT_DIR'], '*.md'))))


class PromptHook(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(BUDGET_JS) and shutil.which("node"), "routing hook not installed")
    def test_no_stage_text_trips_r7(self):
        js = ("const b=require(process.argv[1]);const fs=require('fs');const out={};"
              "for(const f of process.argv.slice(2)){if(!fs.existsSync(f))continue;"
              "const t=fs.readFileSync(f,'utf8');for(const p of b.BUDGET_PATTERNS){const m=t.match(p);if(m)out[f]=m[0];}}"
              "console.log(JSON.stringify(out))")
        hits = json.loads(subprocess.run(["node", "-e", js, BUDGET_JS, *FILES], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(hits, {}, "the routing hook would refuse a stage whose prompt holds these phrases")


if __name__ == "__main__":
    unittest.main()
