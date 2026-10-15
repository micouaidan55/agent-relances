"""Faux client Anthropic : réponses scriptées, aucun appel réseau."""

from types import SimpleNamespace


def reply(parsed, *, input_tokens: int = 1000, output_tokens: int = 200, stop_reason: str = "end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))


class _FakeMessages:
    def __init__(self, responses):
        self._responses = responses if callable(responses) else list(responses)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses(kwargs) if callable(self._responses) else self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)
