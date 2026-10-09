const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const modeScript = [...source.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).find(script => script.includes('const buttons=[...document.querySelectorAll'));
  const section = source.match(/<section class="pgx-mode-panel"[\s\S]*?<\/section>/)[0];
  const dialog = source.match(/<dialog id="pgxModeDialog"[\s\S]*?<\/dialog>/)[0];
  const styles = [...source.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(match => match[1]).join('\n');
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1365]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      let mode = {configured: true, mode: 'loading', selected_mode: 'video', ready: false,
        switching: false, message: '영상 모드 · 서버 응답 확인 중 (생성 요청은 대기열로 접수)'};
      await page.route('https://h3.test/api/pgx-mode', route => route.fulfill({json: {ok: true, mode}}));
      await page.setContent(`<base href="https://h3.test/"><style>${styles}</style>${section}${dialog}
        <script>function pollWhileVisible(fn){fn();}</script><script>${modeScript}</script>`);
      const video = page.locator('[data-pgx-mode="video"]');
      const qwen = page.locator('[data-pgx-mode="qwen"]');
      await page.waitForFunction(() => document.querySelector('[data-pgx-mode="video"]').getAttribute('aria-pressed') === 'true');
      assert.equal(await video.isDisabled(), true);
      assert.equal(await qwen.isDisabled(), false);
      assert.match(await page.locator('#pgxModeStatus').innerText(), /대기열/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.PGX_SCREENSHOT_DIR) {
        await page.screenshot({path: path.join(process.env.PGX_SCREENSHOT_DIR, `pgx-video-loading-${width}.png`)});
      }
      mode = {configured: true, mode: 'qwen', selected_mode: 'qwen', ready: true, switching: false, message: 'Qwen 준비'};
      await page.evaluate(() => document.dispatchEvent(new Event('pgx-mode-refresh')));
      await page.waitForFunction(() => document.querySelector('[data-pgx-mode="qwen"]').getAttribute('aria-pressed') === 'true');
      assert.equal(await video.isDisabled(), false);
      assert.equal(await qwen.isDisabled(), true);
      mode = {configured: true, mode: 'switching', switching: true, message: '모델 전환 중'};
      await page.evaluate(() => document.dispatchEvent(new Event('pgx-mode-refresh')));
      await page.waitForFunction(() => document.getElementById('pgxModeStatus').textContent === '모델 전환 중');
      assert.equal(await video.isDisabled(), true);
      assert.equal(await qwen.isDisabled(), true);
      await page.close();
      console.log(`PGX mode UI: ${width}px passed (loading video / Qwen / switching)`);
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
