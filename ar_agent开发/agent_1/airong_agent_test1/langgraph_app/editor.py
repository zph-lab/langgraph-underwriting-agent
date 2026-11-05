"""Host-only offline editor validation. No upload/save endpoints or model tool."""
import asyncio
import json
import re
from pathlib import Path

import httpx
import yaml


def editor_parts(text):
    """Adapt four-section chat text to the editor web client's folded protocol."""
    sections = {name: [] for name in ('预定义', '如果', '那么', '否则')}
    current = None
    for line in text.splitlines():
        line = line.strip()
        match = re.match(r'^(预定义|如果|那么|否则)\s*[:：]\s*(.*)$', line)
        if match:
            current, line = match.groups()
        if line:
            if current is None:
                raise ValueError('规则正文缺少明确段名')
            if current in ('那么', '否则'):
                line = re.sub(r'^\d+[.)、]\s*', '', line)
            sections[current].append(line)
    if not sections['如果'] or not sections['那么']:
        raise ValueError('规则缺少完整如果/那么正文')
    folded = []
    if sections['预定义']:
        folded += ['预定义 ' + sections['预定义'][0], *sections['预定义'][1:]]
    folded.append('如果 ' + ' '.join(sections['如果']))
    for name in ('那么', '否则'):
        folded += [name + ' ' + line for line in sections[name]]
    return {'ruleDesc': ' '.join(sections['如果']), 'ruleDescRaw': '\n'.join(folded),
            'predefineContent': '\n'.join(sections['预定义']),
            'thenText': '\n'.join(sections['那么']), 'elseText': '\n'.join(sections['否则']),
            'ruleId': 'LOCAL_VALIDATION_ONLY'}


class RuleEditor:
    def __init__(self, profile):
        self.profile = profile

    async def review(self, answer):
        # Only explicitly headed rule blocks, never notes or attachment instructions.
        blocks = list(re.finditer(r'^(`{3,}|~{3,})[^\n]*\n(.*?)^\1\s*$', answer, re.M | re.S))
        blocks = [b for b in blocks if re.search(r'^\s*如果\s*[:：]', b[2], re.M)]
        if not blocks:
            return answer, 'no_rule'
        notices = []
        try:
            async with asyncio.timeout(25):
                async with httpx.AsyncClient(base_url=self.profile['url'].rstrip('/'),
                                             timeout=10, follow_redirects=False, trust_env=False) as client:
                    login_path = self.profile.get('login_config')
                    if login_path:
                        # Explicit local profile points to the user's existing config.
                        # Never return config, response bodies, headers or exceptions.
                        cfg = yaml.safe_load(Path(login_path).read_text(encoding='utf-8'))
                        credentials = {k: cfg['login'][k] for k in ('username', 'password')}
                        response = await client.post('/api/login', json=credentials)
                        if response.status_code != 200:
                            return answer + '\n\n编辑器校验未完成：登录失败，规则尚未通过编译验证。', 'unavailable'
                    for index, block in enumerate(blocks, 1):
                        # Match the editor's normal paste path before folding.
                        response = await client.get('/api/normalize_rule', params={'text': block[2]})
                        response.raise_for_status()
                        normalized = response.json().get('text')
                        if not isinstance(normalized, str) or not normalized.strip():
                            raise ValueError('empty normalized rule')
                        parts = editor_parts(normalized)
                        response = await client.post('/api/generate_rule', json=parts)
                        if response.status_code == 401:
                            return answer + '\n\n编辑器校验未完成：需要有效登录，规则尚未通过编译验证。', 'unavailable'
                        response.raise_for_status()
                        data = response.json()
                        if data.get('success') is True and isinstance(data.get('generated'), dict):
                            notices.append(f'第{index}条：编辑器离线语法与参数生成通过。')
                        elif data.get('success') is False:
                            # Report only explicit compiler diagnostics; no server paths.
                            bad = data.get('invalidWords')
                            if isinstance(bad, list) and bad:
                                snippets = list(dict.fromkeys(str(w.get('text', ''))[:100] for w in bad if isinstance(w, dict)))
                                notices.append(f'第{index}条仍为草稿，编辑器未通过的片段：' + '；'.join(snippets) + '。')
                            else:
                                notices.append(f'第{index}条仍为草稿，编辑器未完成参数生成，需在编辑器核对。')
                        else:
                            raise ValueError('unexpected compiler response')
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, OSError, yaml.YAMLError):
            return answer + '\n\n编辑器校验未完成：服务或配置不可用，规则尚未通过编译验证。', 'unavailable'
        passed = all('仍为草稿' not in note for note in notices)
        return (('规则草稿（编辑器校验未通过）：\n\n' if not passed else '') + answer + '\n\n编辑器校验：\n' + '\n'.join(notices) + (
            '\n以上仅为离线语法与参数生成检查，未执行或保存规则。' if passed else '')), 'passed' if passed else 'invalid'


def load_editor_profile(path):
    path = Path(path)
    if not path.exists():
        return None
    profile = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(profile, dict) or not isinstance(profile.get('url'), str):
        raise ValueError('规则编辑器配置需提供 url')
    return profile
