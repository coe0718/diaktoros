"""Private host helper; never copied or mounted into a seat's sandbox."""
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import types


def main():
    source, plugin = sys.argv[1:]
    sys.path.insert(0, source)
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=Path(os.environ['HERMES_HOME']))
    # Load only installed transport modules, not arbitrary client kwargs from config or
    # request data. The package shim preserves DirectSDK's relative imports.
    package = types.ModuleType('_review_loop_directsdk')
    package.__path__ = [plugin]
    sys.modules[package.__name__] = package
    module = importlib.import_module(package.__name__ + '.directsdk')
    # Profile-selected native login stays host-side. No API key, proxy, hook, Node/Python
    # preload or ambient provider environment reaches the native process.
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'LANG', 'TMPDIR') if key in os.environ}
    command = os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND', 'claude')
    path = shutil.which(command, path=env.get('PATH', '') + ':' + str(Path(env['HOME']) / '.local/bin'))
    if not path:
        raise ValueError('native Claude CLI unavailable')
    config = os.environ.get('CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR') or os.environ.get('CLAUDE_CONFIG_DIR')
    if config:
        if not Path(config).is_absolute() or not Path(config).is_dir():
            raise ValueError('native config directory unavailable')
        env['CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'] = config
    client = module.Client(command=path, args=[], env=env, timeout=120)

    def cancel(*_):
        client.close()
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        payload = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
        if 'reasoning' in payload:
            extra = dict(payload.get('extra_body') or {})
            extra['reasoning'] = payload.pop('reasoning')
            payload['extra_body'] = extra
        response = client.chat.completions.create(**payload)
        if payload.get('stream'):
            try:
                for chunk in response:
                    data = chunk.model_dump()
                    sys.stdout.write('data: ' + json.dumps(data, allow_nan=False) + '\n\n')
                    sys.stdout.flush()
                sys.stdout.write('data: [DONE]\n\n')
            finally:
                response.close()
        else:
            sys.stdout.write(json.dumps(response.model_dump(), allow_nan=False))
        sys.stdout.flush()
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # A class name is safe for a host-side diagnostic; messages/paths/native stderr
        # never cross the capability. Production intentionally discards this stderr.
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
