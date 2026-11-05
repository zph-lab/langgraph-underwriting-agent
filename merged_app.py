"""Unified ASGI entry: one process and one HTTP port for both applications."""
from contextlib import asynccontextmanager
from pathlib import Path
import os
import sys

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import RedirectResponse

ROOT = Path(__file__).resolve().parent
EDITOR_ROOT = ROOT / 'rule_editor_new'
AGENT_ROOT = ROOT / 'ar_agent开发' / 'agent_1' / 'airong_agent_test1'
sys.path[:0] = [str(EDITOR_ROOT), str(AGENT_ROOT)]

from rule_editor.api.main import app as editor_app
from backend.main import app as assistant_app, mgr
from langgraph_app.config import model_config


def merged_model_config():
    config = model_config()
    # Keep the original validation settings, but use this checkout's editor and
    # this server's port instead of the old machine's absolute config path.
    if config.get('rule_editor'):
        config['rule_editor'] = {
            **config['rule_editor'],
            'url': 'http://127.0.0.1:' + os.environ.get('PORT', '8009'),
            'login_config': str(EDITOR_ROOT / 'rule_editor' / 'config.yaml'),
        }
    return config


mgr.config_factory = merged_model_config


@asynccontextmanager
async def lifespan(app):
    # Mounted FastAPI apps do not automatically run their own lifespan.
    async with editor_app.router.lifespan_context(editor_app):
        async with assistant_app.router.lifespan_context(assistant_app):
            yield


app = FastAPI(title='核保工作台', lifespan=lifespan)


@app.get('/assistant', include_in_schema=False)
async def assistant_redirect(request: Request):
    query = '?' + request.url.query if request.url.query else ''
    return RedirectResponse('/assistant/' + query)


# More specific mount must precede the editor's root mount.
app.mount('/assistant', assistant_app, name='assistant')
app.mount('/', editor_app, name='editor')
