"""Replay saved outputs to editor only; no model calls or rule saves."""
import asyncio
import json
import re
from pathlib import Path
import httpx
import yaml
import argparse
from langgraph_app.config import model_config
from langgraph_app.editor import editor_parts

OUT = Path(__file__).parent / 'four-rules-20261030-093525'

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    target = args.output
    profile = model_config()['rule_editor']
    async with httpx.AsyncClient(base_url=profile['url'].rstrip('/'), timeout=25, trust_env=False) as client:
        if profile.get('login_config'):
            config = yaml.safe_load(Path(profile['login_config']).read_text(encoding='utf-8'))
            response = await client.post('/api/login', json={k:config['login'][k] for k in ('username','password')})
            response.raise_for_status()
        for i in range(1,5):
            saved = json.loads((target / f'case-{i}.json').read_text(encoding='utf-8'))
            block = re.search(r'```[^\n]*\n(.*?)```', saved['answer'], re.S)[1]
            response = await client.get('/api/normalize_rule', params={'text':block})
            response.raise_for_status()
            normalized = response.json()['text']
            response = await client.post('/api/generate_rule', json=editor_parts(normalized))
            response.raise_for_status()
            data = response.json()
            safe = {'case':i,'normalized':normalized,'success':data.get('success'),'invalidWords':data.get('invalidWords'),
                    'response_keys':list(data)}
            (target / f'case-{i}-editor-detail.json').write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(safe, ensure_ascii=False), flush=True)

asyncio.run(main())
