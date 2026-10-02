"""Inert sandbox wire profile: HTTP only; no native SDK or host login access."""
from providers import register_provider
from providers.base import ProviderProfile


class WireProfile(ProviderProfile):
    def build_api_kwargs_extras(self, *, reasoning_config=None, **_):
        return ({'reasoning': dict(reasoning_config)} if reasoning_config else {}), {}


register_provider(WireProfile(
    name='review-loop-directsdk-wire',
    display_name='Review loop bounded DirectSDK wire',
    api_mode='chat_completions', auth_type='api_key',
    env_vars=('OPENAI_API_KEY',),
    base_url='http://127.0.0.1:18761/v1',
    native_reasoning_details_type='claude-subscription-directsdk-experimental.native_assistant',
))
