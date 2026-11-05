// Structured adapter shared by LangGraph and MCP. Query logic stays in the original cores.
import readline from 'node:readline';
import * as bom from './bom-search.mjs';
import * as rules from './rule-search.mjs';

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let request;
  try {
    request = JSON.parse(line);
    const { name, arguments: args } = request;
    if (!['bom_search', 'rule_search'].includes(name)) throw new Error('Tool denied');
    const core = name === 'bom_search' ? bom : rules;
    try {
      const result = name === 'bom_search'
        ? await bom.searchFields(args.keyword, { ...args, dbPath: process.env.BOM_SQLITE_PATH ?? bom.DEFAULT_DB_PATH })
        : await rules.searchRules(args.keyword, args);
      process.stdout.write(JSON.stringify({ id: request.id, content: [{ type: 'text', text: core.formatResults(result) }], details: result }) + '\n');
    } catch (error) {
      const info = core.describeError(error);
      // Never send raw connection diagnostics across the model-facing interface.
      process.stdout.write(JSON.stringify({ id: request.id, content: [{ type: 'text', text: info.friendly }], details: { error: true, kind: info.kind }, isError: true }) + '\n');
    }
  } catch {
    process.stdout.write(JSON.stringify({ id: request?.id, content: [{ type: 'text', text: '工具请求无效' }], isError: true }) + '\n');
  }
}
bom.closeDatabase();
await rules.closePool();
