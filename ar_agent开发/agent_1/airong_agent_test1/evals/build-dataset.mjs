import fs from 'node:fs';
import crypto from 'node:crypto';
import {searchRules,formatResults,getPool,closePool,loadConfig} from '../tools/rule-search.mjs';
import {searchFields,closeDatabase,DEFAULT_DB_PATH} from '../tools/bom-search.mjs';
const out=new URL('./private/',import.meta.url);
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const cases=[],sources=[], rejected=[];
const seeds=[
 ['高保额设备业务管控规则','查询设备险保额达到500万元时的业务管控规则。','金额含等号、险别前导零'],
 ['费折联动手续费上限后处理','查一下费折联动中，销售费用A与A3相加超过45时的后处理规则。','表达式、严格大于'],
 ['四川_都江堰摩托车健康险项目','查询四川都江堰摩托车健康险项目规则，保留日期边界和方案号。','日期左闭右开'],
 ['四川_新津补充医疗','查四川新津补充医疗的规则。','机构和代理代码'],
 ['团意险高风险业务管控','查询团意险涉及矿山、钢铁等高风险企业的管控规则。','否定、例外、集合'],
 ['四川非机动车第三者责任险自核_047703','查四川047703非机动车第三者责任险的自核条件。','列表变量、多项限额'],
 ['2025遂宁_道路危险货运承运人责任险（鹏程700元）','查遂宁鹏程道路危险货运承运人责任险700元方案的规则。','历史规则、保费等值'],
 ['湖北分公司电动车项目','查湖北分公司电动车项目，保留保险期间条件的原有逻辑。','原文保真、不擅自纠错'],
 ['温州060032学生平安保险B款自核_方案二','查温州学生平安保险B款方案二，不要和方案一混淆。','相似规则消歧'],
 ['新疆分公司个代渠道非机动车第三责任险','查新疆个人代理渠道非机动车第三责任险规则。','渠道限定']
];
const add=(task,input,source,expected,tags=[])=>cases.push({id:`UW-${String(cases.length+1).padStart(3,'0')}`,task,input,source_group:source,split:'pilot_review',annotation_status:'draft_requires_business_review',expected,tags,forbidden:['编造字段、条件或动作','向业务答复暴露内部字段路径、表名或内部ID']});
try {
 for(const [keyword,input,tag] of seeds){
   const result=await searchRules(keyword,{limit:30,includeContent:true});
   const item=result.items.find(x=>x.ruleName.trim()===keyword.trim());
   if(!item||!item.ifText||!item.actions.length||item.actions.some(a=>!a.methodName||a.nameVariants>1)){rejected.push({keyword,reason:'missing_or_ambiguous_reference'});continue;}
   const source=`rule:${item.ruleId}`;
   const reference=formatResults({...result,items:[item],matchedCount:1,truncated:false,overflow:false}).split('\n\n').slice(1).join('\n\n');
   sources.push({source,keyword,item,reference});
   add('rule_retrieval',input,source,{target_rule_ids:[item.ruleId],k:6,relevance_set_complete:false},[tag]);
 }
 for(const src of sources.slice(0,6)) add('rule_display',`请完整展示“${src.item.ruleName}”的预定义、如果、那么和实际存在的否则内容，不改写动作参数。`,src.source,{reference:src.reference,critical:['所有原有条件及逻辑','数值、单位、比较符','动作分支、顺序、参数','不存在的分支不得补写']});
 for(const keyword of ['保险金额','二级机构','投保日期','共保比例','险别代码','每人赔偿限额','保险期间','保费合计']){
   const result=await searchFields(keyword,{limit:50,dbPath:process.env.BOM_SQLITE_PATH??DEFAULT_DB_PATH});
   const item=result.items.find(x=>x.label===keyword && x.dataType && x.fieldPath);
   if(!item) {rejected.push({keyword,reason:'no_exact_typed_field'});continue;}
   const source=`bom:${item.fieldPath}`;
   add('bom_mapping',`请查询“${item.qualifiedName}”的字段类型，说明完整所属对象。`,source,{fieldPath:item.fieldPath,dataType:item.dataType,type:item.type,qualifiedName:item.qualifiedName,ambiguity_candidates:result.items.filter(x=>x.label===keyword).map(x=>({fieldPath:x.fieldPath,qualifiedName:x.qualifiedName,dataType:x.dataType}))},['父链与同名字段消歧']);
 }
 for(const src of sources.slice(0,4)) add('rule_authoring',`请将以下业务约束规范成公司规则文本，核实字段依据，不改变业务语义：\n${src.reference.replace(/^RULE_ID=.*\n/,'')}`,src.source,{reference:src.reference,critical:['字段必须有BOM依据','全部条件、逻辑、数值及动作保持一致'],boundary_variants:'对原文每个数值阈值检查低于、等于、高于三个情形；只检查条件语义，不声称执行原引擎'},['数据库规则派生、非独立真实新需求']);
 for(const src of sources.slice(0,2)) add('document_extraction',`以下是评测人员由数据库规则整理的模拟条款，不是原始业务文件。提取其中规则并保留来源段号。\n第1段：${src.reference.replace(/^RULE_ID=.*\n/,'')}\n第2段：本条款解释材料，不构成额外规则。`,src.source,{reference:src.reference,expected_rule_count:1,evidence_location:'第1段',critical:['不得把解释材料新增为规则','来源可追溯','条件及动作完整']},['合成文档冒烟、非真实文档验收']);
 add('clarification','帮我写一条高保额业务转人工规则，金额和核保层级你看着定。','synthetic:missing',{must:['询问金额阈值、适用对象和核保层级','未澄清前不输出确定阈值或层级']});
 const missing='评测不存在条款ZXQ20261023';
 const negative=await searchRules(missing,{limit:6});
 if(negative.items.length) throw new Error('negative_not_empty');
 add('no_result',`查找名称中包含“${missing}”的现有规则。`,'synthetic:no-result',{exact_keyword:missing,observed_matches:0,must:['限定为本次检索未命中','不得编造规则','不得声称全库绝无相关规则']});
 // Independent raw snapshot: not solely trusting the production formatter as gold.
 const pool=await getPool(), ids=sources.map(x=>x.item.ruleId), ph=ids.map(()=>'?').join(',');
 const raw={};
 if(ids.length){
   [raw.rules]=await pool.execute(`SELECT RULE_ID,RULE_NAME,RULE_DESC,rule_content,UPDATED_DATE FROM rule_defined WHERE RULE_ID IN (${ph})`,ids);
   [raw.predefines]=await pool.execute(`SELECT RULE_ID,RULE_PREDEFINE_DESC,RULE_PREDEFINE_CONTENT FROM rule_defined_predefine WHERE RULE_ID IN (${ph})`,ids);
   [raw.actions]=await pool.execute(`SELECT RULE_ID,RULE_METHOD_DEFINE_ID,method_id,SETTING_THEN,ORDER_BY FROM rule_method_define WHERE RULE_ID IN (${ph})`,ids);
   [raw.params]=await pool.execute(`SELECT p.* FROM rule_method_define_param p JOIN rule_method_define a ON a.RULE_METHOD_DEFINE_ID=p.RULE_METHOD_DEFINE_ID WHERE a.RULE_ID IN (${ph})`,ids);
 }
 fs.writeFileSync(new URL('sources.json',out),JSON.stringify({sources,raw},null,2));
 fs.writeFileSync(new URL('dataset.jsonl',out),cases.map(x=>JSON.stringify(x)).join('\n')+'\n');
 const counts=Object.fromEntries([...new Set(cases.map(x=>x.task))].map(t=>[t,cases.filter(x=>x.task===t).length]));
 const manifest={version:'pilot-2026-10-23-v1',captured_at:new Date().toISOString(),scope:loadConfig().ruleSearch.bomBaseInfoId,counts,total:cases.length,source_rules:sources.length,rejected,sha256:{dataset:hash(fs.readFileSync(new URL('dataset.jsonl',out))),sources:hash(fs.readFileSync(new URL('sources.json',out)))},limitations:['定向抽样，非全库代表性随机样本','业务口径待人工审核','未完整标注相关规则全集，不计算Recall','同源案例须同组划分开发和验收','尚未调用模型执行质量或性能评测']};
 fs.writeFileSync(new URL('manifest.json',out),JSON.stringify(manifest,null,2));
 fs.writeFileSync(new URL('review.md',out),'# 样本审核表\n\n标注均待业务审核。参考答案和原始快照见 sources.json；同源案例不得跨开发/验收集。\n\n'+cases.map(c=>`## ${c.id} · ${c.task}\n\n来源：${c.source_group}\n\n输入：${c.input}\n\n核对标准：\n\n\x60\x60\x60json\n${JSON.stringify(c.expected,null,2)}\n\x60\x60\x60\n\n审核：待审核；审核人：____；日期：____；修改说明：____\n`).join('\n'));
 console.log(JSON.stringify({total:cases.length,counts,source_rules:sources.length,rejected},null,2));
}catch(e){console.error(JSON.stringify({error:e.code??e.cause?.code??'build_failed'}));process.exitCode=1;}
finally{await closePool();closeDatabase();}
