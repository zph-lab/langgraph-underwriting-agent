// 直接运行现有表单字段收集函数，检查动态域排除及不同参数身份。
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const src = fs.readFileSync(require('node:path').join(__dirname, '../rule_editor/web/app.js'), 'utf8');
function extract(name) {
  const start = src.indexOf('  function ' + name + '(');
  const end = src.indexOf('\n  }', start) + 4;
  assert.ok(start >= 0 && end > start, name);
  return src.slice(start, end);
}
const ctx = vm.createContext({});
vm.runInContext('const blankMethodKeys = new Set();\n' +
  ['localFieldKey', 'isLocalEnum', 'collectOperandOps', 'pushFieldOp'].map(extract).join('\n') + `
  const out = [];
  collectOperandOps({nodeType:'FIELD', dataType:'field', fieldPath:'dynamicsAttributeList.Csubdpt.Cdptcde_0250010000000'}, out);
  for (const code of ['030018','030001','030018']) {
    collectOperandOps({nodeType:'FIELD', fieldPath:'appbase.getCoverage().namt',
      label:'保单的险别0, 险别()的保额/限额(元)',
      exprNode:{paramValue:{param1:{fieldPath:'dynamicsAttributeList.Ccvrgno.StaticProperty_'+code, label:code}}}}, out);
  }
  globalThis.result = out;
`, ctx);
assert.equal(ctx.result.length, 2);
assert.notEqual(ctx.result[0].fp, ctx.result[1].fp);
assert.ok(ctx.result[0].label.includes('030018'));
assert.ok(ctx.result[1].label.includes('030001'));
console.log('Local field collection passed');
