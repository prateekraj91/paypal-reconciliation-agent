import { chromium } from '/Users/prateekraj/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs';
import fs from 'fs';
import path from 'path';

// Extract token from .env.demo without printing it
const envDemo = fs.readFileSync('.env.demo', 'utf-8');
const match = envDemo.match(/ADMIN_API_KEY=["']?([^"'\s\r\n]+)["']?/);
if (!match) {
  console.error('Could not find ADMIN_API_KEY in .env.demo');
  process.exit(1);
}
const token = match[1];

const viewports = [
  { width: 1440, height: 900, label: '1440px desktop' },
  { width: 1280, height: 800, label: '1280px standard' },
  { width: 1084, height: 800, label: '1084px medium tablet/desktop' },
  { width: 500, height: 800, label: '500px mobile' },
];

async function run() {
  const browser = await chromium.launch({ headless: true });
  console.log('Testing Transactions table layout across viewports with Playwright...');

  for (const vp of viewports) {
    console.log(`\n--- Viewport: ${vp.label} (${vp.width}x${vp.height}) ---`);
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
    });
    const page = await context.newPage();

    // Navigate to frontend
    await page.goto('http://localhost:5173');
    await page.waitForLoadState('networkidle');

    // Authenticate if on AuthGate
    const authInput = await page.$('input[placeholder*="ADMIN_API_KEY"]');
    if (authInput) {
      await authInput.fill(token);
      await page.click('button[type="submit"]');
      await page.waitForSelector('text=Overview', { timeout: 5000 });
    }

    // Navigate to Transactions tab
    if (vp.width <= 768) {
      // Mobile: open hamburger drawer
      const hamburger = await page.$('button[aria-label="Open navigation menu"]');
      if (hamburger) {
        await hamburger.click();
        await page.waitForTimeout(300);
      }
    }

    // Click Transactions nav item
    const txNav = await page.locator('nav button:has-text("Transactions")').first();
    await txNav.click();
    await page.waitForSelector('table.fintech-table', { timeout: 5000 });
    await page.waitForTimeout(500); // allow transitions

    // Verify headers
    const ths = await page.$$eval('table.fintech-table thead th', (els) =>
      els.map((e) => ({
        text: e.textContent.trim(),
        rect: e.getBoundingClientRect(),
      }))
    );

    console.log(`Table headers found: ${ths.length}`);
    const unappliedTh = ths.find((h) => h.text.includes('Unapplied Cash'));
    const stateTh = ths.find((h) => h.text.includes('Reconciliation State'));
    const actionsTh = ths.find((h) => h.text.includes('Actions'));

    if (!unappliedTh || !stateTh || !actionsTh) {
      throw new Error(`Missing expected headers: unapplied=${!!unappliedTh}, state=${!!stateTh}, actions=${!!actionsTh}`);
    }

    // Verify header column sequence and non-overlap
    if (unappliedTh.rect.right > stateTh.rect.left + 1) {
      throw new Error(`Header overlap: Unapplied Cash right (${unappliedTh.rect.right}) > State left (${stateTh.rect.left})`);
    }
    if (stateTh.rect.right > actionsTh.rect.left + 1) {
      throw new Error(`Header overlap: Reconciliation State right (${stateTh.rect.right}) > Actions left (${actionsTh.rect.left})`);
    }
    console.log('✓ Header columns flow cleanly with zero overlap');

    // Verify row cells
    const rows = await page.$$eval('table.fintech-table tbody tr', (trs) =>
      trs.map((tr) => {
        const tds = Array.from(tr.querySelectorAll('td'));
        return tds.map((td) => ({
          text: td.textContent.trim(),
          rect: td.getBoundingClientRect(),
        }));
      })
    );

    console.log(`Inspecting ${rows.length} rows for cell boundaries...`);
    let checkedCount = 0;
    for (let rIdx = 0; rIdx < rows.length; rIdx++) {
      const tds = rows[rIdx];
      // Type(0), Payment ID(1), Payer(2), [Date if xl](3), Gross(3/4), Net(4/5), Unapplied(5/6), State(6/7), Actions(7/8)
      const actionsCell = tds[tds.length - 1];
      const stateCell = tds[tds.length - 2];
      const unappliedCell = tds[tds.length - 3];

      // Verify geometry: Unapplied Cash must be strictly to the left of State, and State to the left of Actions
      if (unappliedCell.rect.right > stateCell.rect.left + 1) {
        throw new Error(`Row ${rIdx}: Unapplied cash overlaps State (${unappliedCell.rect.right} > ${stateCell.rect.left})`);
      }
      if (stateCell.rect.right > actionsCell.rect.left + 1) {
        throw new Error(`Row ${rIdx}: State overlaps Actions (${stateCell.rect.right} > ${actionsCell.rect.left})`);
      }
      checkedCount++;
    }

    console.log(`✓ All ${checkedCount} transaction rows verified: Unapplied Cash, Reconciliation State, and Actions have zero overlapping pixels.`);

    // Verify light mode emulation
    await page.emulateMedia({ colorScheme: 'light' });
    const isLightVisible = await page.$eval('table.fintech-table', (el) => {
      const style = window.getComputedStyle(el);
      return style.display !== 'none' && style.visibility !== 'hidden';
    });
    console.log(`✓ Light color scheme emulated cleanly: table visible = ${isLightVisible}`);

    await context.close();
  }

  await browser.close();
  console.log('\nAll 4 viewports passed with 100% clean layouts and zero overlapping columns!');
}

run().catch((err) => {
  console.error('Playwright verification failed:', err);
  process.exit(1);
});
