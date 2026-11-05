# 运行日志与模型截断排查

2026-10-27上下文治理增加 `phase=context` 的原始/模型侧消息字符规模；各模型阶段记录累计 `modelRequests`、`requestChars`（含工具Schema）和 `inputTokenUpperBound`（UTF-8保守容量上界，不是实际token）。模型日志新增 `firstOutputMs`（首个正文/思考增量，纯工具调用可能null）及 `httpAttempts`。请求标准流式usage后，仍以服务实际返回为准。

任务默认600秒总期限、16次generate调用（包含摘要和修正，HTTP重试另计），可通过 `AGENT_TASK_TIMEOUT_SECONDS`、`AGENT_MODEL_REQUEST_LIMIT` 配置正整数。转换连续两批缓存重复或8批检索后进入禁用工具的草稿收尾。上下文、真实token回放和有限真实配对测量见 [优化记录](OPTIMIZATION_2026-10-27.md)。

## 本项目日志

2026-10-27新增诊断通过 `uvicorn.error` 输出到启动终端，不自动保存文件。重启后生效。无需开启DEBUG，不记录业务正文、思考正文、工具结果、地址或密钥。

在PowerShell中运行以下命令，可同时监听局域网并保存日志；先停止已有服务，不要重复占用8712：

```powershell
cd "C:\Users\admin\Desktop\ar_agent开发\agent_1\airong_agent_test1"
New-Item -ItemType Directory -Force .\logs | Out-Null
$agentLog = ".\logs\web-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
& .\.venv-langgraph\Scripts\python.exe -m uvicorn backend.main:app --app-dir . --host 0.0.0.0 --port 8712 2>&1 | Tee-Object -FilePath $agentLog
```

另一个PowerShell窗口查看诊断：

```powershell
cd "C:\Users\admin\Desktop\ar_agent开发\agent_1\airong_agent_test1"
$agentLatestLog = Get-ChildItem .\logs\web-*.log | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $agentLatestLog.FullName -Tail 100 -Wait | Select-String 'agent_diagnostic'
```

每轮用 `sid`、`runId` 关联，每次模型请求另有 `requestId`。`responseId` 是服务实际返回的ID（若有），本地requestId不保证出现在远程日志中。`timestampUtc` 为UTC时间。

重点查看 `stage=end` 的 `model_diagnostic`：

| 字段 | 含义 |
| --- | --- |
| phase | model普通生成、delivery_repair交付修正、summary历史摘要 |
| httpStatus | 最近一次响应HTTP状态；尚未响应时可能没有 |
| maxTokens | 本次请求提交的输出上限，不保证服务端实际采用 |
| inputChars | 消息JSON字符数，不是精确输入token数，也不含工具Schema |
| textChars / thinkingChars | 实际收到的正文/思考字符数，不是token数 |
| toolArgumentChars | 收到的工具参数字符数 |
| finishReason | 服务返回的结束原因；length表示服务报告长度截断，null表示未收到结束原因 |
| usage | 服务实际返回的token用量；没有返回则为null，不以字符数冒充token数 |
| success / durationMs | 适配器是否成功完成、本次请求耗时（包括适配器等待/重试） |

`finishReason=length`且thinkingChars很大、textChars很小：可怀疑模型持续生成思考，但仍需usage和服务日志核实是否耗尽额度。正文和思考都少时，核查服务输出上限、实际上下文上限、网关和请求配置，不仅凭length断言一定生成了32768 tokens。HTTP成功也不表示最终输出完整。

`task_metric`补充工具、摘要、交付修正阶段耗时；工具事件包含cached。日志不是token流转储，不含完整推理正文。

## 转换过程查看

每个助手框上方可点击“处理过程 / 模型返回的思考”。默认折叠，可展开滚动；转换流中服务实际返回的reasoning_content与中间正文都会进入此处，草稿尚未完成交付检查，最终规则仍显示在框内正文。失败、截断或取消后保留已接收过程。浏览器会像现有消息一样保存这些过程文本，受原localStorage容量限制。

这里只展示服务实际返回的文字，无法还原服务未返回的内部推理。内容可辅助排错，不表示模型思考或结论一定正确。摘要过程不展示历史业务全文。

## 模型服务端日志

这些日志必须在实际运行模型服务的机器上查看，本项目Windows终端日志不能代替GPU推理服务日志。部署方式未核实时，不假定容器名、服务名或日志路径。

如果由Docker启动，在模型部署机器上执行（将“实际容器名”替换为真实名称）：

```bash
docker ps --format '{{.Names}}\t{{.Image}}'
docker logs --since 20m --timestamps -f 实际容器名
```

如果由Linux systemd管理：

```bash
journalctl -u 实际服务名 --since '20 minutes ago' -f -o short-iso
```

如果在终端直接启动，查看原启动终端或原启动命令配置的日志文件。按同一时间段、responseId（若服务日志记录）核对请求的输入token、max_tokens、实际完成token、上下文上限、finish_reason及排队/异常。没有这些信息时，需由部署维护人员核查该版本的日志配置。

vLLM不同版本的日志开关存在差异，按安装版本的help核对；新版官方说明区分INFO请求信息与DEBUG提示词内容，排查时优先使用不打印业务正文的INFO配置，不直接开启完整请求正文日志。参考：[vLLM serve官方说明](https://docs.vllm.ai/en/latest/cli/serve/)。
