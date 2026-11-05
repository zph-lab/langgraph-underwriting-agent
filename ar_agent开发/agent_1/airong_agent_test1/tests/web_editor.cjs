const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let app, copied, fail = false;
const context = {
  Vue: {
    createApp: def => ({mount: () => {app = def.setup();}}),
    ref: value => ({value}), reactive: value => value,
    computed: get => ({get value() {return get();}}),
    onMounted() {}, onBeforeUnmount() {}, nextTick: f => f(), watch() {},
  },
  localStorage: {getItem: () => null, setItem() {}},
  navigator: {clipboard: {writeText: async text => {if (fail) throw Error('denied'); copied = text;}}},
  setTimeout: () => 1, clearTimeout() {},
  crypto: require('node:crypto'), console, Date, Math,
};
vm.runInNewContext(fs.readFileSync('web/app.js', 'utf8'), context);
(async () => {
  assert.equal(app.editorOpened.value, false);
  const messages = app.messages.value;
  app.draft.value = '未发送的草稿';
  app.busy.value = true;
  const text = '预定义\n如果 字段 是 "{原值}"\n那么 动作\n否则 其他动作';
  await app.copyToEditor(text);
  assert.equal(copied, text, 'clipboard preserves all original text');
  assert.equal(app.activeView.value, 'editor');
  assert.equal(app.editorOpened.value, true);
  app.showAssistant();
  assert.equal(app.activeView.value, 'assistant');
  assert.equal(app.editorOpened.value, true, 'editor remains mounted');
  assert.equal(app.messages.value, messages);
  assert.equal(app.draft.value, '未发送的草稿');
  assert.equal(app.busy.value, true, 'switch does not cancel task');
  fail = true;
  await app.copyToEditor(text);
  assert.match(app.editorNotice.value, /复制失败/);
  assert.equal(app.activeView.value, 'editor');
  const html = fs.readFileSync('web/index.html', 'utf8');
  assert.match(html, /v-show="activeView === 'assistant'"/);
  assert.match(html, /v-if="editorOpened"/);
  console.log('Editor navigation and clipboard checks passed');
})().catch(err => {console.error(err); process.exitCode = 1;});
