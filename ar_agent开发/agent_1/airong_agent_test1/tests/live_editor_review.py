"""Explicit offline editor review; never calls upload/save endpoints."""
import argparse
import json
import re
from pathlib import Path

import httpx
import yaml
from langgraph_app.editor import editor_parts
from langgraph_app.authoring import canonicalize_setter_spacing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--login-config', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8009')
    parser.add_argument('--output', required=True)
    parser.add_argument('--bom-evidence')
    args = parser.parse_args()
    login = yaml.safe_load(Path(args.login_config).read_text(encoding='utf-8'))['login']
    text = Path(args.source).read_text(encoding='utf-8')
    if args.bom_evidence:
        details = json.loads(Path(args.bom_evidence).read_text(encoding='utf-8-sig'))
        text = canonicalize_setter_spacing(text, [{'details': details}])
    blocks = re.findall(r'^```[^\n]*\n(.*?)^```\s*$', text, re.M | re.S)
    text = blocks[0] if blocks else text
    with httpx.Client(base_url=args.url, timeout=30, follow_redirects=False) as client:
        response = client.post('/api/login', json={k: login[k] for k in ('username', 'password')})
        if response.status_code != 200:
            raise SystemExit('Editor login failed')
        response = client.get('/api/normalize_rule', params={'text': text})
        response.raise_for_status()
        normalized = response.json()['text']
        if not normalized.strip():
            raise SystemExit('Editor returned empty text')
        parts = editor_parts(normalized)
        response = client.get('/api/annotate', params={'text': parts['ruleDescRaw'], 'predefines': parts['predefineContent']})
        response.raise_for_status()
        words = response.json()['words']
        report = {'normalized': normalized, 'invalidWords': [
            {k: w[k] for k in ('text', 'error') if k in w}
            for w in words if w.get('valid') is False]}
        response = client.post('/api/generate_rule', json=parts)
        response.raise_for_status()
        data = response.json()
        report['compileSuccess'] = data.get('success') is True
        report['compileError'] = data.get('errMsg')
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
