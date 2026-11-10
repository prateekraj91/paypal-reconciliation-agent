import { chromium } from '/Users/prateekraj/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs';
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';

const TOKEN = process.env.DEMO_ADMIN_API_KEY;
if (!TOKEN || !TOKEN.trim()) {
  throw new Error('DEMO_ADMIN_API_KEY environment variable is required.');
}
const RECORDING_DIR = '/tmp/demo_recording_raw';
const OUTPUT_MP4 = path.resolve('paypal_reconciliation_demo.mp4');

// Clean previous recordings
fs.rmSync(RECORDING_DIR, { recursive: true, force: true });
fs.mkdirSync(RECORDING_DIR, { recursive: true });

// Create the browser wrapper HTML
const wrapperHtml = `
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Reconciliation Agent | Demo</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #09090b;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    overflow: hidden;
    width: 1440px;
    height: 900px;
    user-select: none;
  }
  #header {
    height: 74px;
    background: #18181b;
    border-bottom: 1px solid #27272a;
    display: flex;
    flex-direction: column;
    position: relative;
    z-index: 100;
  }
  #tab-row {
    height: 38px;
    display: flex;
    align-items: center;
    padding-left: 14px;
    gap: 6px;
    background: #141416;
  }
  .window-dots {
    display: flex;
    gap: 8px;
    margin-right: 14px;
  }
  .dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
  }
  .dot-red { background: #ef4444; border: 1px solid #dc2626; }
  .dot-yellow { background: #f59e0b; border: 1px solid #d97706; }
  .dot-green { background: #10b981; border: 1px solid #059669; }
  
  .tab {
    height: 32px;
    padding: 0 14px;
    display: flex;
    align-items: center;
    gap: 9px;
    border-radius: 7px 7px 0 0;
    font-size: 11.5px;
    color: #a1a1aa;
    cursor: pointer;
    background: #18181b;
    border-top: 1px solid transparent;
    border-left: 1px solid transparent;
    border-right: 1px solid transparent;
    transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    max-width: 290px;
  }
  .tab.active {
    background: #09090b;
    color: #fafafa;
    font-weight: 500;
    border-top: 1px solid #3f3f46;
    border-left: 1px solid #27272a;
    border-right: 1px solid #27272a;
    box-shadow: 0 -2px 6px rgba(0,0,0,0.4);
  }
  .tab-icon {
    width: 15px;
    height: 15px;
    background: linear-gradient(135deg, #3f3f46, #27272a);
    border: 1px solid #52525b;
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 8px;
    font-weight: 700;
    color: #e4e4e7;
    flex-shrink: 0;
  }
  .tab-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    letter-spacing: -0.01em;
  }
  .tab-close {
    font-size: 13px;
    color: #71717a;
    margin-left: 4px;
    opacity: 0.6;
  }
  
  #nav-row {
    height: 36px;
    display: flex;
    align-items: center;
    padding: 0 14px;
    gap: 12px;
    background: #18181b;
  }
  .nav-controls {
    display: flex;
    align-items: center;
    gap: 10px;
    color: #71717a;
  }
  #url-bar {
    flex: 1;
    max-width: 820px;
    height: 26px;
    background: #09090b;
    border: 1px solid #27272a;
    border-radius: 6px;
    display: flex;
    align-items: center;
    padding: 0 10px;
    gap: 8px;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    color: #e4e4e7;
    margin: 0 auto;
  }
  #url-text {
    letter-spacing: -0.02em;
  }
  
  #frames-container {
    position: absolute;
    top: 74px;
    left: 0;
    right: 0;
    bottom: 0;
    background: #000;
  }
  iframe {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: none;
    transition: opacity 0.2s ease;
  }
  #frame1 {
    opacity: 1;
    z-index: 2;
  }
  #frame2 {
    opacity: 1;
    z-index: 1;
  }
  
  #cursor {
    position: fixed;
    top: 0;
    left: 0;
    width: 22px;
    height: 22px;
    pointer-events: none;
    z-index: 9999999;
    transform: translate(400px, 200px);
    will-change: transform;
    filter: drop-shadow(0 2px 5px rgba(0,0,0,0.6));
  }
  .ripple {
    position: fixed;
    width: 26px;
    height: 26px;
    border-radius: 50%;
    border: 2px solid rgba(255, 255, 255, 0.9);
    pointer-events: none;
    z-index: 9999998;
    transform: translate(-50%, -50%) scale(0.2);
    animation: rip 0.45s cubic-bezier(0.16, 1, 0.3, 1) forwards;
  }
  @keyframes rip {
    to { transform: translate(-50%, -50%) scale(2.2); opacity: 0; }
  }
  
  #pre-overlay {
    position: fixed;
    inset: 0;
    background: #0a0a0a;
    z-index: 99999999;
    pointer-events: none;
    transition: opacity 0.35s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #71717a;
    font-size: 12px;
    font-family: monospace;
  }
</style>
</head>
<body>
  <div id="pre-overlay">INITIALIZING RECONCILIATION WORKSPACE...</div>
  
  <div id="header">
    <div id="tab-row">
      <div class="window-dots">
        <div class="dot dot-red"></div>
        <div class="dot dot-yellow"></div>
        <div class="dot dot-green"></div>
      </div>
      <div id="tab1" class="tab active">
        <div class="tab-icon">PR</div>
        <span class="tab-title">Reconciliation Agent | Operator Dashboard</span>
        <span class="tab-close">×</span>
      </div>
      <div id="tab2" class="tab">
        <div class="tab-icon">PR</div>
        <span class="tab-title">Reconciliation Agent | Operator Dashboard</span>
        <span class="tab-close">×</span>
      </div>
    </div>
    
    <div id="nav-row">
      <div class="nav-controls">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
      </div>
      <div id="url-bar">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
        <span id="url-text">http://127.0.0.1:5173</span>
      </div>
    </div>
  </div>

  <div id="frames-container">
    <iframe id="frame1" src="http://127.0.0.1:5173"></iframe>
    <iframe id="frame2" src="http://127.0.0.1:8001"></iframe>
  </div>

  <div id="cursor">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
      <path d="M3 3l7.5 18 3-7.5 7.5-3L3 3z" fill="#ffffff" stroke="#000000" stroke-width="1.8" stroke-linejoin="round"/>
    </svg>
  </div>

  <script>
    window.currentCursorX = 400;
    window.currentCursorY = 200;
    
    window.moveCursor = function(targetX, targetY, durationMs = 500) {
      return new Promise((resolve) => {
        const cursor = document.getElementById('cursor');
        const startX = window.currentCursorX;
        const startY = window.currentCursorY;
        const startTime = performance.now();
        
        function animate(now) {
          const elapsed = now - startTime;
          const p = Math.min(elapsed / durationMs, 1);
          // easeInOutCubic
          const ease = p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
          const curX = startX + (targetX - startX) * ease;
          const curY = startY + (targetY - startY) * ease;
          cursor.style.transform = 'translate(' + curX + 'px, ' + curY + 'px)';
          window.currentCursorX = curX;
          window.currentCursorY = curY;
          
          if (p < 1) {
            requestAnimationFrame(animate);
          } else {
            resolve();
          }
        }
        requestAnimationFrame(animate);
      });
    };

    window.clickRipple = function() {
      const ripple = document.createElement('div');
      ripple.className = 'ripple';
      ripple.style.left = window.currentCursorX + 'px';
      ripple.style.top = window.currentCursorY + 'px';
      document.body.appendChild(ripple);
      setTimeout(() => ripple.remove(), 450);
    };

    window.activateTab1 = function() {
      document.getElementById('frame1').style.opacity = '1';
      document.getElementById('frame1').style.pointerEvents = 'auto';
      document.getElementById('frame1').style.zIndex = '2';
      document.getElementById('frame2').style.opacity = '0';
      document.getElementById('frame2').style.pointerEvents = 'none';
      document.getElementById('frame2').style.zIndex = '1';
    };

    window.switchToTab2 = function() {
      document.getElementById('tab1').classList.remove('active');
      document.getElementById('tab2').classList.add('active');
      document.getElementById('url-text').textContent = 'http://127.0.0.1:8001';
      document.getElementById('frame1').style.opacity = '0';
      document.getElementById('frame1').style.pointerEvents = 'none';
      document.getElementById('frame2').style.opacity = '1';
      document.getElementById('frame2').style.pointerEvents = 'auto';
      document.getElementById('frame2').style.zIndex = '3';
    };
  </script>
</body>
</html>
`;

fs.writeFileSync('/tmp/demo_wrapper.html', wrapperHtml);

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function moveAndClick(page, frameId, selector, opts = {}) {
  const dur = opts.dur || 600;
  const frame = page.frameLocator(frameId);
  const loc = frame.locator(selector).first();
  await loc.waitFor({ state: 'visible', timeout: 8000 });
  const box = await loc.boundingBox();
  if (!box) throw new Error('Could not get box for ' + selector);
  
  const targetX = box.x + box.width / 2;
  const targetY = 74 + box.y + box.height / 2;
  await page.evaluate(({ x, y, d }) => window.moveCursor(x, y, d), { x: targetX, y: targetY, d: dur });
  await sleep(dur + 60);
  await page.evaluate(() => window.clickRipple());
  await loc.click();
}

async function moveAndHover(page, frameId, selector, dur = 600) {
  const frame = page.frameLocator(frameId);
  const loc = frame.locator(selector).first();
  await loc.waitFor({ state: 'visible', timeout: 8000 });
  const box = await loc.boundingBox();
  if (!box) return;
  const targetX = box.x + box.width / 2;
  const targetY = 74 + box.y + box.height / 2;
  await page.evaluate(({ x, y, d }) => window.moveCursor(x, y, d), { x: targetX, y: targetY, d: dur });
  await sleep(dur + 60);
}

async function scrollModal(page, frameId, targetY, dur = 600) {
  const frame = page.frameLocator(frameId);
  await frame.locator('body').evaluate(async (body, { ty, d }) => {
    const dialog = document.querySelector('.modal-dialog');
    if (!dialog) return;
    const startY = dialog.scrollTop || 0;
    const startT = performance.now();
    await new Promise(res => {
      function step(now) {
        const el = now - startT;
        const p = Math.min(el / d, 1);
        const ease = p < 0.5 ? 2 * p * p : -1 + (4 - 2 * p) * p;
        dialog.scrollTop = startY + (ty - startY) * ease;
        if (p < 1) requestAnimationFrame(step);
        else res();
      }
      requestAnimationFrame(step);
    });
  }, { ty: targetY, d: dur });
  await sleep(dur + 60);
}

async function scrollFrame(page, frameId, targetY, dur = 800) {
  const frame = page.frameLocator(frameId);
  await frame.locator('body').evaluate(async (body, { ty, d }) => {
    const scroller = document.querySelector('main') || document.documentElement || window;
    const startY = scroller.scrollTop || 0;
    const startT = performance.now();
    await new Promise(res => {
      function step(now) {
        const el = now - startT;
        const p = Math.min(el / d, 1);
        const ease = p < 0.5 ? 2 * p * p : -1 + (4 - 2 * p) * p;
        const curY = startY + (ty - startY) * ease;
        if (scroller.scrollTop !== undefined) scroller.scrollTop = curY;
        if (window.scrollTo) window.scrollTo(0, curY);
        if (p < 1) requestAnimationFrame(step);
        else res();
      }
      requestAnimationFrame(step);
    });
  }, { ty: targetY, d: dur });
  await sleep(dur + 60);
}

async function main() {
  console.log('Launching headless browser at 1440x900 for video recording...');
  const browser = await chromium.launch({
    headless: true,
  });

  const context = await browser.newContext({
    recordVideo: {
      dir: RECORDING_DIR,
      size: { width: 1440, height: 900 }
    },
    viewport: { width: 1440, height: 900 }
  });

  const page = await context.newPage();
  const recordingInitTime = Date.now();
  await page.goto('file:///tmp/demo_wrapper.html');
  await sleep(1500);

  // Authenticate both iframes silently behind the pre-overlay
  console.log('Authenticating Frame 1 (5173)...');
  const f1 = page.frameLocator('#frame1');
  const input1 = f1.locator('input[placeholder*="ADMIN_API_KEY"]');
  if (await input1.count() > 0) {
    await input1.fill(TOKEN);
    await input1.press('Enter');
    await f1.locator('button:has-text("Overview")').first().waitFor({ timeout: 10000 });
  }

  console.log('Authenticating Frame 2 (8001)...');
  const f2 = page.frameLocator('#frame2');
  const input2 = f2.locator('input[placeholder*="ADMIN_API_KEY"]');
  if (await input2.count() > 0) {
    await input2.fill(TOKEN);
    await input2.press('Enter');
    await f2.locator('button:has-text("Overview")').first().waitFor({ timeout: 10000 });
  }

  // Ensure Tab 1 is visible and Tab 2 is hidden
  await page.evaluate(() => window.activateTab1());
  await sleep(500);

  console.log('Both tabs authenticated and ready!');
  console.log('Starting recording take! Removing pre-overlay...');
  
  // Note the exact timestamp when recording begins (0:00)
  const recordingStartTime = Date.now();
  const preOverlayOffsetSec = (recordingStartTime - recordingInitTime) / 1000;
  console.log(`Pre-overlay duration: ${preOverlayOffsetSec.toFixed(3)}s (will be trimmed)`);

  await page.evaluate(() => {
    const ov = document.getElementById('pre-overlay');
    if (ov) ov.remove();
  });

  const getElapsed = () => (Date.now() - recordingStartTime) / 1000;
  async function waitTargetTime(targetSec) {
    const elapsed = getElapsed();
    const waitMs = (targetSec - elapsed) * 1000;
    if (waitMs > 0) {
      await sleep(waitMs);
    }
  }

  // ==========================================
  // BEAT 1: 0:00–0:15 (15s)
  // Tab 1, tenant = Demo Merchant Corp, overview screen.
  // ==========================================
  console.log('[0:00] Beat 1: Tab 1, tenant = Demo Merchant Corp, overview screen');
  await sleep(3500);
  
  // Hover over Active Tenant dropdown in sidebar
  await moveAndHover(page, '#frame1', 'aside select', 700);
  await sleep(2500);
  
  // Hover over key metric cards on overview
  await moveAndHover(page, '#frame1', 'text=Open Exceptions', 800);
  await sleep(2500);
  
  await moveAndHover(page, '#frame1', 'text=Open Payment Exceptions', 800);
  await sleep(2000);

  await moveAndHover(page, '#frame1', 'text=Recent AI Exception Cases', 800);
  await waitTargetTime(15.0);

  // ==========================================
  // BEAT 2: 0:15–0:45 (30s)
  // Switch Active Tenant to Test Tenant.
  // Open Invoices & Ledger; show RECON-T-901 marked PAID.
  // Open Transactions; click 89H37763BV8635251; show Reference RECON-T-901.
  // Switch tenant back to Demo Merchant Corp.
  // ==========================================
  console.log('[0:15] Beat 2: Multi-tenant demonstration');
  // Switch to Test Tenant
  await moveAndHover(page, '#frame1', 'aside select', 500);
  await page.evaluate(() => window.clickRipple());
  await f1.locator('aside select').selectOption('test_tenant');
  await sleep(1500);

  // 0:21 - Open Invoices & Ledger
  await waitTargetTime(21.0);
  await moveAndClick(page, '#frame1', 'button:has-text("Invoices & Ledger")', { dur: 600 });
  await sleep(1000);
  
  // Hover over RECON-T-901 row and PAID badge
  await moveAndHover(page, '#frame1', 'text=RECON-T-901', 600);
  await sleep(2000);
  await moveAndHover(page, '#frame1', 'text=PAID ($0.00)', 600);
  await sleep(1500);

  // 0:29 - Open Transactions
  await waitTargetTime(29.0);
  await moveAndClick(page, '#frame1', 'button:has-text("Transactions")', { dur: 600 });
  await sleep(1000);

  // Click 89H37763BV8635251
  await moveAndClick(page, '#frame1', 'text=89H37763BV8635251', { dur: 600 });
  await sleep(1000);

  // Detail modal is open! Hover over Reference: RECON-T-901
  await moveAndHover(page, '#frame1', 'text=RECON-T-901', 600);
  await sleep(3500);

  // 0:40 - Close modal and switch back to Demo Merchant Corp
  await waitTargetTime(40.0);
  // Close modal via backdrop click
  await f1.locator('div.fixed.inset-0.z-50').first().click({ position: { x: 20, y: 20 } });
  await sleep(800);

  // Switch tenant back to demo_merchant
  await moveAndHover(page, '#frame1', 'aside select', 500);
  await page.evaluate(() => window.clickRipple());
  await f1.locator('aside select').selectOption('demo_merchant');
  await sleep(800);

  await waitTargetTime(45.0);

  // ==========================================
  // BEAT 3: 0:45–1:10 (25s)
  // Review Queue, show only the top two rows.
  // Open Evidence on one. If the modal opens low, scroll down to it.
  // ==========================================
  console.log('[0:45] Beat 3: Review Queue exceptions');
  await moveAndClick(page, '#frame1', 'button:has-text("Review Queue")', { dur: 600 });
  await sleep(1500);

  // Hover across top two rows
  await moveAndHover(page, '#frame1', 'text=PAY-DEMO-INJECTION-011', 700);
  await sleep(2500);
  await moveAndHover(page, '#frame1', 'text=PAY-DEMO-INVESTIGATE-010', 700);
  await sleep(2500);

  // Open Evidence on row 2 (PAY-DEMO-INVESTIGATE-010)
  const row2 = f1.locator('tr:has-text("PAY-DEMO-INVESTIGATE-010")');
  const evBtn = row2.locator('button:has-text("Evidence")');
  const evBox = await evBtn.boundingBox();
  if (evBox) {
    const targetX = evBox.x + evBox.width / 2;
    const targetY = 74 + evBox.y + evBox.height / 2;
    await page.evaluate(({ x, y }) => window.moveCursor(x, y, 500), { x: targetX, y: targetY });
    await sleep(550);
    await page.evaluate(() => window.clickRipple());
    await evBtn.click();
  }
  await sleep(1200);

  // Modal open! Scroll down inside modal if needed
  await scrollModal(page, '#frame1', 220, 800);
  await sleep(2500);

  // Close modal via backdrop click
  await f1.locator('.modal-overlay').first().click({ position: { x: 20, y: 20 } });
  await sleep(600);

  await waitTargetTime(70.0);

  // ==========================================
  // BEAT 4: 1:10–1:45 (35s)
  // AI Investigations: open top row, invg_640dddf5.
  // Show dashed warning advisory panel and Verified Facts.
  // Scroll down to the locked Approve button.
  // ==========================================
  console.log('[1:10] Beat 4: AI Investigations & Verified Facts');
  await moveAndClick(page, '#frame1', 'button:has-text("AI Investigations")', { dur: 600 });
  await sleep(1500);

  // Click top row (invg_640dddf5)
  await moveAndClick(page, '#frame1', 'text=invg_640dddf5', { dur: 600 });
  await sleep(1500);

  // Show dashed warning advisory panel (Model-raised flags)
  await moveAndHover(page, '#frame1', 'text=Model-raised flags', 700);
  await sleep(3500);

  // Show Verified Facts
  await moveAndHover(page, '#frame1', 'text=Verified Facts', 700);
  await sleep(4000);

  // Scroll down smoothly to the locked Approve button at the bottom
  console.log('Scrolling down to locked Approve button...');
  await scrollFrame(page, '#frame1', 1200, 1500);
  const approveLockedBtn = f1.locator('button:has-text("Approve Proposed Resolution")');
  await approveLockedBtn.scrollIntoViewIfNeeded();
  await sleep(800);

  // Hover over the locked Approve Proposed Resolution button (DO NOT CLICK!)
  const albBox = await approveLockedBtn.boundingBox();
  if (albBox) {
    const targetX = albBox.x + albBox.width / 2;
    const targetY = 74 + albBox.y + albBox.height / 2;
    await page.evaluate(({ x, y }) => window.moveCursor(x, y, 700), { x: targetX, y: targetY });
    await sleep(750);
  }
  await sleep(4000);

  await waitTargetTime(105.0);

  // ==========================================
  // BEAT 5: 1:45–2:20 (35s)
  // Review Queue: open PAY-DEMO-INJECTION-011 row, show Approve locked,
  // then open its Evidence showing "APPROVAL STRICTLY PROHIBITED".
  // ==========================================
  console.log('[1:45] Beat 5: Prompt injection defense');
  await moveAndClick(page, '#frame1', 'button:has-text("Review Queue")', { dur: 600 });
  await sleep(1000);
  await scrollFrame(page, '#frame1', 0, 300);
  await sleep(500);

  // Focus on PAY-DEMO-INJECTION-011 row
  await moveAndHover(page, '#frame1', 'text=PAY-DEMO-INJECTION-011', 700);
  await sleep(2500);

  // Show Approve locked on this row
  const injRow = f1.locator('tr:has-text("PAY-DEMO-INJECTION-011")');
  const lockedBtn = injRow.locator('[data-testid="approve-locked"]');
  const lBox = await lockedBtn.boundingBox();
  if (lBox) {
    const targetX = lBox.x + lBox.width / 2;
    const targetY = 74 + lBox.y + lBox.height / 2;
    await page.evaluate(({ x, y }) => window.moveCursor(x, y, 600), { x: targetX, y: targetY });
    await sleep(650);
  }
  await sleep(3000);

  // Open its Evidence button
  const injEvBtn = injRow.locator('button:has-text("Evidence")');
  const injEvBox = await injEvBtn.boundingBox();
  if (injEvBox) {
    const targetX = injEvBox.x + injEvBox.width / 2;
    const targetY = 74 + injEvBox.y + injEvBox.height / 2;
    await page.evaluate(({ x, y }) => window.moveCursor(x, y, 500), { x: targetX, y: targetY });
    await sleep(550);
    await page.evaluate(() => window.clickRipple());
    await injEvBtn.click();
  }
  await sleep(1500);

  // Evidence modal open! Hover over APPROVAL STRICTLY PROHIBITED
  await moveAndHover(page, '#frame1', 'text=APPROVAL STRICTLY PROHIBITED', 700);
  await sleep(5000);

  // Close modal via backdrop click
  await f1.locator('.modal-overlay').first().click({ position: { x: 20, y: 20 } });
  await sleep(1000);

  await waitTargetTime(140.0);

  // ==========================================
  // BEAT 6: 2:20–2:45 (25s)
  // Switch to Tab 2 (8001). Approve PAY-DEMO-EXACT-001.
  // ==========================================
  console.log('[2:20] Beat 6: Switch to Tab 2 (8001) and Approve PAY-DEMO-EXACT-001');
  
  // Move cursor to Tab 2 button in top bar
  const tab2Box = await page.locator('#tab2').boundingBox();
  const t2X = tab2Box.x + tab2Box.width / 2;
  const t2Y = tab2Box.y + tab2Box.height / 2;
  await page.evaluate(({ x, y }) => window.moveCursor(x, y, 600), { x: t2X, y: t2Y });
  await sleep(650);
  await page.evaluate(() => {
    window.clickRipple();
    window.switchToTab2();
  });
  await sleep(1000);

  // Ensure Tab 2 is on Review Queue
  await moveAndClick(page, '#frame2', 'button:has-text("Review Queue")', { dur: 500 });
  await sleep(1200);

  // Find PAY-DEMO-EXACT-001 row
  const exactRow = f2.locator('tr:has-text("PAY-DEMO-EXACT-001")');
  await exactRow.waitFor({ state: 'visible', timeout: 5000 });
  await exactRow.scrollIntoViewIfNeeded();
  await sleep(600);
  const exactApproveBtn = exactRow.locator('button:has-text("Approve")');
  
  // Move to Approve button
  const aBox = await exactApproveBtn.boundingBox();
  if (aBox) {
    const targetX = aBox.x + aBox.width / 2;
    const targetY = 74 + aBox.y + aBox.height / 2;
    await page.evaluate(({ x, y }) => window.moveCursor(x, y, 600), { x: targetX, y: targetY });
    await sleep(650);
    await page.evaluate(() => window.clickRipple());
    await exactApproveBtn.click();
  }
  await sleep(1500);

  // Approval modal opens!
  await moveAndHover(page, '#frame2', 'text=Independent Verifier Status: VERIFIED', 600);
  await sleep(1500);

  // Click Confirm & Authorize Allocation
  await moveAndClick(page, '#frame2', 'button:has-text("Confirm & Authorize Allocation")', { dur: 600 });
  console.log('PAY-DEMO-EXACT-001 approved!');
  await sleep(2500);

  await waitTargetTime(165.0);

  // ==========================================
  // BEAT 7: 2:45–3:00 (15s)
  // Show the Audit Trail.
  // ==========================================
  console.log('[2:45] Beat 7: Show the Audit Trail');
  await moveAndClick(page, '#frame2', 'button:has-text("Audit Trail")', { dur: 600 });
  await sleep(1500);

  // Hover over top audit trail row
  await moveAndHover(page, '#frame2', 'text=sarah.operator@company.com', 700);
  await sleep(3500);

  await moveAndHover(page, '#frame2', 'text=Cryptographic Audit Trail', 700);
  
  await waitTargetTime(180.0);

  console.log('[3:00] Demo recording complete!');
  
  await context.close();
  await browser.close();

  // Find generated webm
  const files = fs.readdirSync(RECORDING_DIR).filter(f => f.endsWith('.webm'));
  if (files.length === 0) throw new Error('No webm video recorded!');
  const rawWebm = path.join(RECORDING_DIR, files[0]);
  console.log('Raw webm recorded at:', rawWebm);

  // Multiplex with audio
  console.log(`Multiplexing video (trimmed by ${preOverlayOffsetSec.toFixed(3)}s) and synced audio into final MP4...`);
  const ffmpegCmd = [
    '/opt/homebrew/bin/ffmpeg', '-y',
    '-i', `"${rawWebm}"`,
    '-i', '"/tmp/demo_audio/master_narration_180s.mp3"',
    '-filter_complex', `"[0:v]trim=start=${preOverlayOffsetSec.toFixed(3)}:duration=180.0,setpts=PTS-STARTPTS[v]"`,
    '-map', '"[v]"',
    '-map', '1:a',
    '-c:v', 'libx264',
    '-preset', 'fast',
    '-crf', '19',
    '-pix_fmt', 'yuv420p',
    '-c:a', 'aac',
    '-b:a', '192k',
    '-t', '180.0',
    `"${OUTPUT_MP4}"`
  ].join(' ');

  execSync(ffmpegCmd, { stdio: 'inherit' });
  console.log('Final video generated successfully at:', OUTPUT_MP4);
}

main().catch(err => {
  console.error('Recording error:', err);
  process.exit(1);
});
