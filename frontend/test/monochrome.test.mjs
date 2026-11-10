import test from 'node:test';
import assert from 'node:assert';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const root = new URL('..', import.meta.url).pathname;
const read = (rel) => readFileSync(join(root, rel), 'utf8');

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

const CHROMATIC = 'red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|gray|zinc|stone';
const colourSources = [
  ...walk(join(root, 'src')).filter((p) => /\.(tsx?|css)$/.test(p)),
  join(root, 'index.html'),
  join(root, 'tailwind.config.js'),
  join(root, 'public/favicon.svg'),
];

function isGrey(r, g, b) {
  return r === g && g === b;
}

test('no chromatic Tailwind colour classes remain in the source', () => {
  const re = new RegExp(`(?<![\\w-])(?:[\\w-]+:)*(?:bg|text|border(?:-[trblxy])?|ring|from|to|via|fill|stroke|shadow|outline|divide|placeholder|accent|caret|decoration)-(?:${CHROMATIC})-\\d+`, 'g');
  const hits = colourSources.flatMap((p) => (readFileSync(p, 'utf8').match(re) || []).map((m) => `${p}: ${m}`));
  assert.deepStrictEqual(hits, []);
});

test('every hex and rgb colour in the source is a neutral grey', () => {
  const hits = [];
  for (const p of colourSources) {
    const s = readFileSync(p, 'utf8');
    for (const m of s.matchAll(/#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])/g)) {
      const h = m[1].length === 3 ? m[1].split('').map((c) => c + c).join('') : m[1];
      const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
      if (!isGrey(r, g, b)) hits.push(`${p}: ${m[0]}`);
    }
    for (const m of s.matchAll(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/g)) {
      if (!isGrey(+m[1], +m[2], +m[3])) hits.push(`${p}: ${m[0]}`);
    }
  }
  assert.deepStrictEqual(hits, []);
});

test('Tailwind palette is locked to black, white and neutral', () => {
  const config = read('tailwind.config.js');
  assert.match(config, /theme:\s*{\s*colors:\s*{/);
  assert.doesNotMatch(config, /extend:\s*{\s*colors/);
  // `gray` is allowed only as the single preflight placeholder shade, pinned to neutral.
  assert.match(config, /gray: \{ 400: neutral\[400\] \}/);
  assert.doesNotMatch(config, new RegExp(`\\b(${CHROMATIC.replace('|gray', '')})\\s*:`));
  assert.match(config, /DEFAULT: '#ffffff'/);
});

const css = read('src/index.css');
const rule = (selector) => {
  const i = css.indexOf(selector);
  assert.ok(i >= 0, `missing CSS rule ${selector}`);
  return css.slice(i, css.indexOf('}', i));
};

test('INVALID badges are an inverted block with a heavy solid border', () => {
  const r = rule('.badge-unmatched, .badge-invalid');
  assert.match(r, /background-color: #ffffff/);
  assert.match(r, /color: #000000/);
  assert.match(r, /border: 2px solid/);
  assert.match(r, /text-transform: uppercase/);
});

test('verified and reconciled badges use a thin solid border', () => {
  assert.match(rule('.badge-reconciled, .badge-verified'), /border: 1px solid/);
});

test('needs-review and held badges use a double border', () => {
  assert.match(rule('.badge-partial, .badge-needs-review'), /border: 3px double/);
  assert.match(rule('.state-held {'), /border: 3px double/);
});

test('advisory model flags use a dashed border', () => {
  assert.match(rule('.state-advisory {'), /border: 2px dashed/);
});

test('fail-closed banner is an inverted, heavily bordered block', () => {
  const r = rule('.state-fail-closed {');
  assert.match(r, /background-color: #ffffff/);
  assert.match(r, /border: 4px solid #ffffff/);
});

test('disabled buttons never look enabled', () => {
  const r = rule('.btn:disabled,');
  assert.match(r, /background-color: #111111 !important/);
  assert.match(r, /border: 1px dashed/);
  assert.match(r, /cursor: not-allowed/);
});

test('verifier and status badges keep their icon and label', () => {
  const common = read('src/components/Common.tsx');
  assert.match(common, /<ShieldCheck[^>]*\/>\s*VERIFIED/);
  assert.match(common, /<XCircle[^>]*\/>\s*INVALID \(FAIL-CLOSED\)/);
  assert.match(common, /<AlertTriangle[^>]*\/>\s*NEEDS REVIEW/);
  assert.match(common, /badge badge-reconciled">\s*<CheckCircle2/);
  assert.match(common, /badge badge-unmatched">\s*<XCircle/);
});

test('investigation page keeps every verdict label with its icon', () => {
  const page = read('src/pages/InvestigationDetailPage.tsx');
  assert.match(page, /className="state-fail-closed[^"]*"/);
  assert.match(page, /⛔<\/span>\s*<XCircle[^>]*\/>\s*CRITICAL SAFETY ALERT: Independent Verifier Failed Closed \(Verdict: INVALID\)/);
  assert.match(page, /font-extrabold uppercase[^"]*">\s*<span aria-hidden="true"[^>]*>⛔/);
  assert.match(page, /className="state-advisory[^"]*"/);
  assert.match(page, /⚠<\/span>\s*<AlertTriangle[^>]*\/>\s*Model-raised flags \(advisory\)/);
  assert.match(page, /They are not the verifier's verdict/);
  assert.match(page, /'⛔ Approval strictly blocked by safety verifier'/);
  assert.match(page, /\{approvalBlocked && <Lock size=\{12\} \/>\}/);
  assert.match(page, /const FactIcon = isInvalid \? XCircle : CheckCircle2;/);
});

test('review queue keeps the prohibited text and a locked Approve button', () => {
  const modals = read('src/components/Modals.tsx');
  assert.match(modals, /'APPROVAL STRICTLY PROHIBITED \(FAIL-CLOSED\)'/);
  assert.match(modals, /'state-fail-closed text-black'/);
  assert.match(modals, /font-extrabold uppercase text-black/);
  const queue = read('src/pages/ProposalsPage.tsx');
  assert.match(queue, /data-testid="approve-locked"[^]*?<Lock size=\{11\}[^>]*\/>\s*Approve/);
  assert.match(queue, /✕<\/span>\s*PROMPT_INJECTION_SUSPECT/);
});

test('colour-emoji symbols are rendered in greyscale or replaced', () => {
  assert.match(rule('.glyph-mono {'), /filter: grayscale\(1\)/);
  for (const p of colourSources.filter((f) => /\.tsx$/.test(f))) {
    const src = readFileSync(p, 'utf8');
    assert.doesNotMatch(src, /[✅❌]/u, `${p} uses a colour emoji`);
    for (const line of src.split('\n')) {
      if (/[⛔⚡]/u.test(line) && !/glyph-mono/.test(line)) {
        // the glyph must sit inside an element carrying glyph-mono (checked on the opening tag nearby)
        const idx = src.indexOf(line);
        const before = src.slice(Math.max(0, idx - 1200), idx);
        assert.match(before, /glyph-mono/, `${p}: ${line.trim()} is not inside .glyph-mono`);
      }
    }
  }
});

test('PAID invoices carry a check icon in a thin solid-border badge', () => {
  const inv = read('src/pages/InvoicesPage.tsx');
  assert.match(inv, /badge badge-success[^"]*"><CheckCircle2[^>]*\/>PAID \(\$0\.00\)/);
});
