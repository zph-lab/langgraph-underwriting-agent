const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let app;
let reject = false;
let resolvePrompt;
const context = {
  Vue: {
    createApp: def => ({mount: () => { app = def.setup(); }}),
    ref: value => ({value}), reactive: value => value,
    computed: get => ({get value() {return get();}}),
    onMounted: () => {}, onBeforeUnmount: () => {}, nextTick: f => f(), watch: () => {},
  },
  localStorage: {getItem: () => null, setItem: () => {}},
  setTimeout: () => 1, clearTimeout: () => {},
  fetch: async () => { if (reject) throw new Error('offline'); await new Promise(r => {resolvePrompt = r;}); return {ok:true, json:async () => ({ok:true})}; },
  console, Date, Math, crypto: require('node:crypto'),
};
let source = fs.readFileSync('web/app.js', 'utf8');
source = source.replace('return { sidebarOpen,', 'return { handleEvent, sidebarOpen,');
vm.runInNewContext(source, context);
(async () => {
  app.draft.value = '查询机构';
  const pending = app.send();
  assert.equal(app.busy.value, true, 'visible fallback immediately while HTTP pending');
  app.handleEvent({type:'task_status',text:'任务执行中'});
  assert.equal(app.activity.active, true);
  app.handleEvent({type:'tool_execution_start',toolName:'bom_search'});
  assert.match(app.activity.text, /检索/);
  app.handleEvent({type:'tool_execution_end'});
  assert.equal(app.busy.value, true, 'fallback remains visible after tool ends');
  const end = (text, tool = false, thinking = '') => app.handleEvent({type:'message_end', message:{role:'assistant', content:[
    {type:'text',text}, {type:'thinking',thinking}, ...(tool ? [{type:'toolCall',id:'x'}] : [])]}});
  app.handleEvent({type:'message_update',assistantMessageEvent:{type:'text_delta',delta:'核实字段'}});
  end('核实字段', true, '第一轮思考');
  end('继续查询', true, '第二轮思考');
  end('最终规则', false, '最终思考');
  let assistants = app.messages.value.filter(m => m.role === 'assistant');
  assert.equal(assistants.length, 1, 'all tool rounds share one bubble');
  assert.equal(assistants[0].text, '最终规则', 'final-only event renders authoritative answer');
  assert.equal(assistants[0].process, '第一轮思考\n\n核实字段\n\n第二轮思考\n\n继续查询');
  assert.equal(assistants[0].thinking, '最终思考');
  app.handleEvent({type:'agent_settled'});
  assert.equal(app.busy.value || app.activity.active, false);
  assert.equal(assistants[0].streaming, false);
  resolvePrompt(); await pending;
  reject = true;
  app.draft.value = '再次查询';
  await app.send();
  assert.equal(app.busy.value || app.activity.active, false, 'send failure clears status');
  end('第二轮最终答复');
  app.handleEvent({type:'agent_settled'});
  assistants = app.messages.value.filter(m => m.role === 'assistant');
  assert.equal(assistants.length, 2, 'separate user turns do not merge');
  assert.equal(assistants[0].text, '最终规则');
  app.draft.value = '取消轮次';
  await app.send();
  end('取消前检索说明', true);
  app.handleEvent({type:'message_end',message:{role:'assistant',content:[],stopReason:'aborted'}});
  app.handleEvent({type:'agent_settled'});
  assert.equal(app.messages.value.at(-1).process, '取消前检索说明');
  assert.equal(app.messages.value.at(-1).streaming, false);
  app.draft.value = '转换规则';
  await app.send();
  app.handleEvent({type:'message_update',assistantMessageEvent:{type:'process_delta',delta:'尚未验证的草稿与思考'}});
  const processBubble = app.messages.value.at(-1);
  assert.equal(processBubble.text, '', 'conversion drafts stay out of final answer');
  app.handleEvent({type:'message_end',message:{role:'assistant',content:[],stopReason:'error',error:'输出截断'}});
  app.handleEvent({type:'agent_settled'});
  assert.equal(processBubble.process, '尚未验证的草稿与思考', 'failed stream remains inspectable');
  assert.equal(processBubble.streaming, false);
  assert.equal(app.messages.value.at(-1).text, '输出截断', 'no unrelated authentication hint');
  app.draft.value = '按险别转换规则';
  await app.send();
  const before = app.messages.value.filter(m => m.role === 'assistant').length;
  app.handleEvent({type:'message_update',assistantMessageEvent:{type:'process_delta',delta:'已找到保额字段。\n\n'}});
  end('', true);
  app.handleEvent({type:'message_update',assistantMessageEvent:{type:'process_delta',delta:'已核对险别参数绑定。\n\n'}});
  end('', true);
  app.handleEvent({type:'message_update',assistantMessageEvent:{type:'text_delta',delta:'如果: 规范条件\n那么: 保留原动作'}});
  end('如果: 规范条件\n那么: 保留原动作');
  app.handleEvent({type:'agent_settled'});
  assert.equal(app.messages.value.filter(m => m.role === 'assistant').length, before + 1);
  assert.equal(app.messages.value.at(-1).process, '已找到保额字段。\n\n已核对险别参数绑定。\n\n');
  assert.equal(app.messages.value.at(-1).text, '如果: 规范条件\n那么: 保留原动作');
  app.busy.value = true;
  app.handleEvent({type:'bridge_dead'});
  assert.equal(app.busy.value || app.activity.active, false);
  const html = fs.readFileSync('web/index.html', 'utf8');
  assert.match(html, /v-show="busy \|\| activity.active"/);
  assert.match(html, /<details v-if="m.process \|\| m.thinking"/);
  assert.match(html, /处理过程 \/ 简要进度/);
  console.log('Web status lifecycle checks passed');
})();

