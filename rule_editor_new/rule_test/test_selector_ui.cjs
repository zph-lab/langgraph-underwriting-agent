// 前端交互回归：node rule_test/test_selector_ui.cjs（需安装 playwright 和 Edge）。
// 使用模拟接口；真实 BOM 解析/编码/校验由 test_selector_values.py 覆盖。
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://selector.test/**', async route => {
      const url = new URL(route.request().url());
      if (!url.pathname.startsWith('/api/')) {
        const name = url.pathname === '/' ? 'index.html' : url.pathname.split('/').pop();
        return route.fulfill({
          contentType: name.endsWith('.js') ? 'application/javascript' : 'text/html',
          body: fs.readFileSync(path.join(__dirname, '../rule_editor/web', name)),
        });
      }
      let data = {};
      if (url.pathname === '/api/auth_check') data = { authenticated: true };
      if (url.pathname === '/api/boot_id') data = { bootId: 'selector-ui-test' };
      if (url.pathname === '/api/search') data = { candidates:
        url.searchParams.get('prev') === '保单的业务类型' ? [
          { label: '不属于（多选）', kind: 'operator', code: 'STR_NOT_BELONG_TO_MUL' },
        ] : [] };
      if (url.pathname === '/api/annotate') data = { words: [] };
      if (url.pathname === '/api/selector_context') {
        const line = url.searchParams.get('line');
        const caret = Number(url.searchParams.get('caret'));
        const regex = /(不?属于)\s*[（(]\s*(单选|多选)\s*[）)]/g;
        data = { active: false };
        for (const match of line.matchAll(regex)) {
          let start = match.index + match[0].length;
          while (start < line.length && /\s/.test(line[start])) start++;
          let end = line.indexOf(' 并且 ', start);
          if (end < 0) end = line.length;
          if (caret < match.index + match[0].length || caret > end) continue;
          const org = line.slice(0, match.index).trimEnd().endsWith('二级机构');
          data = {
            active: true, start, end, multiple: match[2] === '多选', raw: line.slice(start, end).trim(),
            codeService: org ? 'carCsubdptService' : 'carCbustypeService',
            options: org ? [{ label: '山西', key: '14' }, { label: '北京', key: '11' }]
              : [{ label: '续保', key: '1' }, { label: '转保', key: '2' }, { label: '新保', key: '3' }],
          };
          break;
        }
      }
      return route.fulfill({ json: data });
    });
    await page.goto('http://selector.test/');
    const box = page.locator('#ruleBox');
    await box.fill('如果\n保单的业务类型 ');
    await box.press('End');
    await page.locator('#suggestList').getByText('不属于（多选）', { exact: true }).click();
    await page.locator('#suggestList input[type=checkbox]').first().waitFor();
    for (const label of ['转保', '新保']) {
      await page.locator('#suggestList label').filter({ hasText: label }).locator('input').check();
    }
    await page.getByRole('button', { name: '确认选择', exact: true }).click();
    assert((await box.inputValue()).endsWith('[转保,新保]'));
    await box.click();
    await box.press('End');
    await page.locator('#suggestList input[type=checkbox]').first().waitFor();
    assert.equal(await page.locator('#suggestList input:checked').count(), 2);
    await page.locator('#suggestList input[type=search]').fill('转');
    assert.equal(await page.locator('#suggestList label').count(), 1);
    await page.getByRole('button', { name: '取消', exact: true }).click();
    const adjacent = '如果\n保单的业务类型 不属于（多选） [转保,新保] 并且 保单的保费 大于 100';
    await box.fill(adjacent);
    await box.evaluate(el => {
      const caret = el.value.indexOf('转保');
      el.setSelectionRange(caret, caret);
      el.dispatchEvent(new Event('click'));
    });
    await page.locator('#suggestList input[type=checkbox]').first().waitFor();
    await page.locator('#suggestList label').filter({ hasText: '新保' }).locator('input').uncheck();
    await page.getByRole('button', { name: '确认选择', exact: true }).click();
    assert.equal(await box.inputValue(), adjacent.replace('[转保,新保]', '[转保]'));
    await box.fill('如果\n保单的二级机构 属于（单选） ');
    await box.press('End');
    await page.locator('#suggestList input[type=radio]').first().waitFor();
    assert.equal(await page.locator('#suggestList label').count(), 2);
    await page.locator('#suggestList label').filter({ hasText: '山西' }).click();
    assert((await box.inputValue()).endsWith('山西'));
    assert.deepEqual(errors, []);
    console.log('PASS: 单选、多选、回显、搜索、取消、字段值域隔离；无浏览器异常');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
