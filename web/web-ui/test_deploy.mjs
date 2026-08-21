// SPDX-License-Identifier: MIT
/**
 * Deployer label maths, without a browser.
 *
 * The gauge is the only place a user sees the memory budget before a 409 tells
 * them about it, so an empty-string zero ("· of 2.9 GB in use") is a real bug,
 * not a cosmetic one. Lifts gb/sizeLabel straight out of deploy.html.
 *
 * Run: node web/web-ui/test_deploy.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const page = fs.readFileSync(path.join(DIR, 'deploy.html'), 'utf8');
const script = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).join('\n');

function grab(head) {
  const start = script.indexOf(head);
  if (start < 0) throw new Error('deploy.html no longer defines ' + head);
  let depth = 0;
  for (let i = script.indexOf('{', start); i < script.length; i++) {
    if (script[i] === '{') depth++;
    else if (script[i] === '}' && --depth === 0) {
      const end = script[i + 1] === ';' ? i + 2 : i + 1;
      return script.slice(start, end);
    }
  }
  throw new Error('unbalanced braces in ' + head);
}

const mod = path.join(os.tmpdir(), 'case-deploy-under-test.mjs');
fs.writeFileSync(mod, `
  ${grab('const gb=')}
  ${grab('function sizeLabel(')}
  ${grab('const deskUrl=')}
  export { gb, sizeLabel, deskUrl };
`);
const { gb, sizeLabel, deskUrl } = await import('file://' + mod);

let bad = 0;
const is = (got, want, what) => {
  if (got === want) return console.log('ok ', what);
  bad++;
  console.log('FAIL', what, '→', JSON.stringify(got), 'want', JSON.stringify(want));
};

is(gb(0), '0 GB', 'an empty host reads "0 GB", not ""');
is(gb(2048), '2 GB', 'whole gigabytes lose the decimal');
is(gb(2928), '2.9 GB', 'the derived budget is not a round number');
is(gb(1536), '1.5 GB', 'half a gigabyte survives');
is(gb(null), '', 'unknown is blank, so no "0 GB" ghost appears');
is(gb(undefined), '', 'missing is blank too');

is(sizeLabel({ ram_mb: 2048, cpus: 1 }), '2 GB · 1 CPU', 'one CPU is singular');
is(sizeLabel({ ram_mb: 1024, cpus: 2 }), '1 GB · 2 CPUs', 'more than one is plural');
is(sizeLabel({}), '', 'a row from an older cased shows nothing rather than "0 GB"');

// The gauge line as paint() builds it — the bug the screenshot caught.
const sub = (ram, max) => '1 computer on this host' + (max ? ' · ' + gb(ram) + ' of ' + gb(max) + ' in use' : '');
is(sub(0, 2928), '1 computer on this host · 0 GB of 2.9 GB in use', 'idle host still names the budget');
is(sub(1024, 2928), '1 computer on this host · 1 GB of 2.9 GB in use', 'in-use memory reads back');
is(sub(0, 0), '1 computer on this host', 'no budget (no /proc) says nothing at all');

// DESK opens the same live view Drive embeds (index.html builds this URL too).
is(deskUrl('c_abc123'),
  '/live/c_abc123/vnc.html?autoconnect=1&resize=scale&path=live/c_abc123/websockify',
  'desk url matches the /live proxy contract');
is(page.includes('class="desk"'), true, 'rows render a DESK button');

fs.rmSync(mod, { force: true });
if (bad) { console.log(`\n${bad} deploy label check(s) failed`); process.exit(1); }
console.log('\ndeploy labels OK');
