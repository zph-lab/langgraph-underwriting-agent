"""Run the unified application from any working directory."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='启动核保工作台（编辑器 + AI 助手）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8009')))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port 必须在 1 到 65535 之间')
    os.environ['PORT'] = str(args.port)
    os.chdir(Path(__file__).resolve().parent)
    import uvicorn
    print(f'核保工作台：http://127.0.0.1:{args.port}/', flush=True)
    uvicorn.run('merged_app:app', host=args.host, port=args.port, workers=1)


if __name__ == '__main__':
    main()
