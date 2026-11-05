// Read-only scoped discovery; never print configuration or raw errors.
import fs from 'node:fs';
import {getPool, closePool, loadConfig, stripHtml} from '../tools/rule-search.mjs';
const scope = loadConfig().ruleSearch.bomBaseInfoId;
try {
  const pool = await getPool();
  const [rows] = await pool.execute(`SELECT /*+ MAX_EXECUTION_TIME(15000) */
    rd.RULE_ID id, rd.RULE_NAME name, rd.RULE_DESC description,
    (SELECT COUNT(*) FROM rule_method_define a WHERE a.RULE_ID=rd.RULE_ID) actions,
    (SELECT COUNT(*) FROM rule_method_define a WHERE a.RULE_ID=rd.RULE_ID AND a.SETTING_THEN='0') elseCount
    FROM rule_defined rd WHERE rd.IS_DELETED=0 AND rd.RULE_ID IN (
      SELECT cr.RULE_ID FROM catalogue_rule_rel cr
      JOIN decision_rep_catalogue dc ON dc.DECISION_REP_CATALOGUE_ID=cr.DECISION_REP_CATALOGUE_ID
      JOIN decision_repository dr ON dr.DECISION_REPOSITORY_ID=dc.DECISION_REPOSITORY_ID
      WHERE dr.BOM_BASE_INFO_ID=?)
    AND rd.RULE_NAME IS NOT NULL AND rd.RULE_DESC IS NOT NULL AND rd.RULE_NAME NOT LIKE '%测试%' AND rd.RULE_ID NOT LIKE '%TEST%' AND rd.RULE_ID LIKE 'YA%'
    ORDER BY rd.RULE_ID LIMIT 600`, [scope]);
  const clean=rows.map(r=>({...r,description:stripHtml(r.description)}));
  fs.writeFileSync(new URL('./private/candidates.json',import.meta.url),JSON.stringify(clean,null,2));
  console.log(JSON.stringify({count:clean.length,examples:clean.filter(r=>r.description.length>30&&r.description.length<1800&&r.actions>0).slice(0,45).map(r=>({...r,description:r.description.slice(0,260)}))},null,2));
} catch(e) { console.error(JSON.stringify({error:e.code??e.cause?.code??'query_failed'}));process.exitCode=1; }
finally { await closePool(); }


