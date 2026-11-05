const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const location = {hash: '', pathname: '/', search: '', origin: 'http://localhost:8010'};
const editorHtml = fs.readFileSync(path.join(root, 'rule_editor_new/rule_editor/web/index.html'), 'utf8');
const assistantHtml = fs.readFileSync(path.join(root, 'ar_agent开发/agent_1/airong_agent_test1/web/index.html'), 'utf8');
assert.match(editorHtml, /id="assistantTab" href="\/assistant\/"/);
assert.match(editorHtml, /id="editorTab" href="\/" aria-current="page"/);
assert.ok(!editorHtml.includes('<aside class="feature-sidebar"'));
assert.ok(!editorHtml.includes('<iframe'));
assert.match(assistantHtml, /class="workspace-tab active" href="\.\/" aria-current="page"/);
assert.match(assistantHtml, /class="workspace-tab" :href="editorUrl"/);
let replaced;
vm.runInNewContext(fs.readFileSync(path.join(root, 'rule_editor_new/rule_editor/web/navigation.js'), 'utf8'), {
  location: {hash: '#rule-ai-assistant', search: '?test=1', replace(value) {replaced = value;}}
});
assert.equal(replaced, '/assistant/?test=1');
let app, fetched;
const pageLocation = {...location, pathname: "/assistant/"};
vm.runInNewContext(fs.readFileSync(path.join(root, 'ar_agent开发/agent_1/airong_agent_test1/web/app.js'), 'utf8'), {
  Vue: {createApp: def => ({mount() {app = def.setup();}}), ref: value => ({value}),
    reactive: value => value, computed: get => ({get value() {return get();}}),
    onMounted() {}, onBeforeUnmount() {}, nextTick: f => f(), watch() {}},
  localStorage: {getItem: () => null, setItem() {}},
  navigator: {clipboard: {writeText: async () => {}}},
  location: pageLocation,
  crypto: require('node:crypto'), setTimeout: () => 1, clearTimeout() {},
  fetch: async url => {fetched = url; return {ok: true, json: async () => ({})};},
  console, Date, Math
});
(async () => {
  app.draft.value = '保留草稿'; app.busy.value = true;
  await app.copyToEditor('原始完整答复');
  assert.equal(pageLocation.href, '/');
  assert.equal(app.editorOpened.value, false, 'do not create a nested editor');
  assert.equal(app.draft.value, '保留草稿');
  assert.equal(app.busy.value, true);
  await app.stop();
  assert.equal(fetched, '/assistant/api/abort');
  console.log('Top navigation links, full-page editor redirect, legacy URL and API prefix: PASS');
})().catch(err => {console.error(err); process.exitCode = 1;});
