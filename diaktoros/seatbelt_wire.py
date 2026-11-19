"""Staged Hermes transports: exact per-turn AF_UNIX, never a TCP listener.

Only dummy credentials enter this process. The host capability owns authentication,
wire validation, quota, upstream selection and response bounds.
"""
from pathlib import Path
from diaktoros import envnames

from providers import register_provider
from providers.base import ProviderProfile

NAME = 'diaktoros-seatbelt-wire'
RESPONSES_NAME = 'diaktoros-seatbelt-responses'


def unix_client():
    import httpx
    endpoint = Path(envnames.get('INFERENCE_SOCKET', ''))
    if not endpoint.is_absolute() or not endpoint.is_socket():
        raise RuntimeError('live inference socket required')
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(endpoint), retries=0),
                        trust_env=False, follow_redirects=False, timeout=135)


class UnixWireProfile(ProviderProfile):
    def create_client(self, **client_kwargs):
        from openai import OpenAI
        client = unix_client()
        try:
            return OpenAI(base_url='http://localhost/v1', api_key='sandbox-dummy',
                          default_headers=client_kwargs.get('default_headers'),
                          http_client=client, max_retries=0)
        except BaseException:
            client.close()
            raise


def install_messages_transport():
    # Hermes's Messages builder bypasses ProviderProfile.create_client. Keep its
    # OAuth/header decisions and replace only SDK transport, before Hermes imports.
    import anthropic
    original = anthropic.Anthropic

    class UnixAnthropic(original):
        def __init__(self, **kwargs):
            client = unix_client()
            kwargs.update(http_client=client, base_url='http://localhost/anthropic',
                          max_retries=0)
            try:
                super().__init__(**kwargs)
            except BaseException:
                client.close()
                raise

    anthropic.Anthropic = UnixAnthropic


if __name__ == '__main__':
    import runpy
    import sys
    from diaktoros.native_python_spawn import install
    install()
    if len(sys.argv) > 1 and sys.argv[1] == '--messages':
        install_messages_transport()
        del sys.argv[1]
    if len(sys.argv) < 3 or sys.argv[1] != '--':
        raise SystemExit('usage: python -P -m diaktoros.seatbelt_wire -- HERMES [ARGS...]')
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name='__main__')
else:
    for name, mode in ((NAME, 'chat_completions'), (RESPONSES_NAME, 'codex_responses')):
        register_provider(UnixWireProfile(
            name=name, display_name='Diaktoros native Unix wire', api_mode=mode,
            auth_type='api_key', env_vars=('OPENAI_API_KEY',),
            base_url='http://localhost/v1', supports_model_listing=False,
            supports_health_check=False,
        ))
