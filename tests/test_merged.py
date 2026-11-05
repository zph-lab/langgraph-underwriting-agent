"""Integration checks without model requests or ADP writes."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import merged_app
from rule_editor.api import auth
from rule_editor.config import get


class MergedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_state = merged_app.mgr.state_dir
        merged_app.mgr.state_dir = Path(cls.temp.name)
        cls.client = TestClient(merged_app.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        merged_app.mgr.state_dir = cls.old_state
        cls.temp.cleanup()

    def test_pages_assets_and_lifespan(self):
        for path in ('/', '/static/app.js', '/static/navigation.js', '/static/logo.svg',
                     '/assistant/', '/assistant/app.js', '/assistant/styles.css',
                     '/assistant/vendor/vue.global.prod.js'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get('/assistant').status_code, 200)
        self.assertTrue(self.client.get('/assistant/api/health').json()['persistenceReady'])
        self.assertEqual(self.client.get('/api/health').status_code, 200)

    def test_editor_auth_search_and_generation(self):
        self.client.cookies.clear()
        if auth.enabled():
            self.assertEqual(self.client.get('/api/search?word=保单').status_code, 401)
            response = self.client.post('/api/login', json={
                'username': get('login', 'username'), 'password': get('login', 'password')})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/auth_check').status_code, 200)
        response = self.client.get('/api/search', params={'word': '保单'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json())
        self.assertEqual(self.client.get('/api/annotate', params={
            'text': '如果 保单的个团标志 是 动态域集的个团标志的团单'}).status_code, 200)
        response = self.client.post('/api/generate_rule', json={
            'ruleDesc': '保单的个团标志 是 动态域集的个团标志的团单',
            'ruleId': 'LOCAL_MERGE_TEST'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('success'), 'Offline rule generation failed')

    def test_assistant_api_is_separate(self):
        response = self.client.post('/assistant/api/upload', files={
            'file': ('merge-check.txt', b'original attachment text', 'text/plain')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['text'], 'original attachment text')
        self.assertEqual(self.client.post('/assistant/api/prompt', json={}).status_code, 400)
        self.assertEqual(self.client.post('/assistant/api/close', json={'sid': 'merge-test'}).status_code, 200)

    def test_validation_uses_current_editor(self):
        profile = merged_app.merged_model_config()['rule_editor']
        self.assertEqual(Path(profile['login_config']), merged_app.EDITOR_ROOT / 'rule_editor/config.yaml')


if __name__ == '__main__':
    unittest.main()
