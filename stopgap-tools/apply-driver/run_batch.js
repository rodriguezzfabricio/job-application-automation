const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parseArgs() {
  const args = {};
  for (let i = 2; i < process.argv.length; i++) {
    const a = process.argv[i];
    if (a.startsWith('--')) {
      const k = a.slice(2);
      const v = process.argv[i + 1];
      if (v && !v.startsWith('--')) { args[k] = v; i++; } else args[k] = true;
    }
  }
  return args;
}

function loadJobs(jobsPath) {
  const raw = JSON.parse(fs.readFileSync(path.resolve(jobsPath), 'utf8'));
  if (!Array.isArray(raw)) throw new Error('jobs file must be a JSON array');
  return raw.map((j) => (typeof j === 'string' ? { url: j } : j));
}

async function main() {
  const args = parseArgs();
  if (!args.jobs || !args.private) {
    console.error('usage: node run_batch.js --jobs jobs.json --private DIR [--cooldown-min 10] [--resume FILE]');
    process.exit(2);
  }
  const jobs = loadJobs(args.jobs);
  const cooldownMin = parseInt(args['cooldown-min'] || '10', 10);
  const cooldownMs = Math.max(0, cooldownMin) * 60 * 1000;
  const privateDir = path.resolve(args.private);
  const driver = path.join(__dirname, 'driver.js');
  const defaultResume = args.resume;

  const report = { started_at: new Date().toISOString(), cooldown_min: cooldownMin, results: [] };
  for (let i = 0; i < jobs.length; i++) {
    const job = jobs[i];
    const resume = job.resume || defaultResume;
    if (!resume) throw new Error('Specify the résumé filename for each opening or with --resume');
    console.log(`[${i + 1}/${jobs.length}] ${job.url}`);
    let result;
    try {
      const out = execFileSync('node', [driver, '--job', job.url, '--private', privateDir, '--resume', resume], {
        encoding: 'utf8', timeout: 180000,
      });
      result = JSON.parse(out.trim() || '{}');
    } catch (e) {
      result = { job: job.url, error: (e && e.message || String(e)).slice(0, 200) };
    }
    report.results.push(result);
    console.log('  ->', result.blocked ? `BLOCKED (${result.blocked})` : (result.error ? 'ERROR ' + result.error : 'steps ' + (result.steps || []).join(',')));
    if (result.blocked) {
      console.log(`STOPPING: rate-limited at job ${i + 1}; wait before resuming.`);
      break;
    }
    if (i < jobs.length - 1) {
      console.log(`  cooldown ${cooldownMin} min before next job...`);
      await sleep(cooldownMs);
    }
  }
  const outPath = path.join(privateDir, 'codex-runtime', 'batch-report.json');
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
  console.log('report:', outPath);
}

main().catch((e) => { console.error('BATCH_ERROR:', e && e.message); process.exit(1); });
