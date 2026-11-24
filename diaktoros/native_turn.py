"""Native-only turn staging; broker policy and outcome handling stay in trusted_turn."""
from contextlib import contextmanager
from pathlib import Path
import shutil

from . import native_backend, turn_layout


def write_home(home, model, api_mode, client_identity, reasoning_effort):
    from . import trusted_turn
    if client_identity == 'directsdk':
        raise trusted_turn.TurnDenied('DirectSDK is not supported by the native Unix transport')
    provider = trusted_turn._write_sandbox_home(
        home, model, api_mode, client_identity, reasoning_effort)
    if api_mode in ('chat_completions', 'codex_responses'):
        native_provider = ('diaktoros-seatbelt-wire' if api_mode == 'chat_completions'
                           else 'diaktoros-seatbelt-responses')
        config = home / 'config.yaml'
        text = config.read_text().replace('provider: ' + provider + '\n',
                                          'provider: ' + native_provider + '\n', 1)
        text = text.replace('enabled: []', 'enabled: [' + native_provider + ']')
        text = text.replace('http://127.0.0.1:18761/v1', 'http://localhost/v1')
        config.write_text(text)
        plugin = home / 'plugins' / native_provider
        plugin.mkdir(mode=0o700, parents=True)
        shutil.copyfile(Path(__file__).with_name('seatbelt_wire.py'), plugin / '__init__.py')
        (plugin / 'plugin.yaml').write_text(
            f'name: {native_provider}\nkind: model-provider\nversion: 0.1.0\nmanifest_version: 2\n')
        provider = native_provider
    return provider


@contextmanager
def prepare(*, home, code, venv, checkout, client, query, ledger_db=None):
    # The backend owns the durable ledger-adjacent cleanup pool, not this temporary root.
    options = {'pool': Path(ledger_db).parent.resolve() / 'native-cleanup'} if ledger_db else {}
    with native_backend.workspace(**options) as workspace:
        shutil.copytree(home, workspace.home, dirs_exist_ok=True)
        layout = turn_layout.TurnLayout(code=code.resolve(), venv=Path(venv).resolve(),
            home=workspace.home, work=workspace.work, export=checkout.resolve(),
            client=client.resolve(), scratch=workspace.scratch, query=query.resolve())
        for name in ('seatbelt_wire.py', 'native_python_spawn.py'):
            shutil.copyfile(Path(__file__).with_name(name), client / 'diaktoros' / name)
        yield workspace, layout


def entry(layout, *, provider, model, max_steps, timeout, api_mode='chat_completions'):
    # No HTTP listener: the protected bootstrap installs the Messages Unix transport.
    return [str(layout.venv / 'bin/python'), '-P', '-m', 'diaktoros.seatbelt_wire',
            *(['--messages'] if api_mode == 'anthropic_messages' else []), '--',
            *layout.hermes_entry(provider=provider, model=model, max_steps=max_steps,
                                 timeout=timeout)[1:]]
