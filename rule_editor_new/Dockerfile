# adp 智能规则编辑器 — Docker 部署
# 构建：docker build -t rule-editor:latest .
# 运行：docker run -d -p 8010:8010 --name rule-editor rule-editor:latest
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用构建缓存（代码改动不触发重装依赖）
COPY rule_editor/requirements.txt ./rule_editor/requirements.txt
RUN pip install --no-cache-dir -r rule_editor/requirements.txt

# 后端与前端代码
COPY rule_editor/ ./rule_editor/
# ADP 上传/转换器 + 方法注册表 bom_methods_*.json（运行时懒加载依赖）
COPY desc_to_rulecontent/ ./desc_to_rulecontent/
# 本地求值器 / 测试报文生成器（routes.py 运行时 import）
COPY rule_test/ ./rule_test/
# BOM 数据源（config.yaml data.bom_path 指向 新车险915/）
COPY 新车险915/ ./新车险915/
# ETL 产物（bom_catalog/value_dict/freq_stats/corpus_index 四个 sqlite）
COPY data/ ./data/

EXPOSE 8010

# 健康检查：GET /api/health（换端口时同步设置环境变量 PORT）
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8010')+'/api/health',timeout=4)" || exit 1

# 容器内必须监听 0.0.0.0（config.yaml 里的 127.0.0.1 会被此处参数覆盖）
CMD uvicorn rule_editor.api.main:app --host 0.0.0.0 --port ${PORT:-8010}
