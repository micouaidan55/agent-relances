"""Appel structuré au modèle : sortie validée par Pydantic, une nouvelle tentative, suivi du coût."""

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from agent_relances.models import Usage

WRITER_MODEL = "claude-sonnet-5"
JUDGE_MODEL = "claude-sonnet-5"
PRICES_PER_MTOK = {"claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-5": (2.00, 10.00)}
MAX_ATTEMPTS = 2

T = TypeVar("T", bound=BaseModel)


class StepError(RuntimeError):
    def __init__(self, message: str, usage: Usage):
        super().__init__(message)
        self.usage = usage


def usage_of(model: str, response) -> Usage:
    tokens_in, tokens_out = response.usage.input_tokens, response.usage.output_tokens
    price_in, price_out = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return Usage(input_tokens=tokens_in, output_tokens=tokens_out,
                 cost_usd=(tokens_in * price_in + tokens_out * price_out) / 1_000_000)


def call_structured(client, *, model: str, system: str, content: str, output_model: type[T],
                    max_tokens: int = 4096) -> tuple[T, Usage]:
    usage = Usage()
    last_problem = ""
    for _ in range(MAX_ATTEMPTS):
        try:
            response = client.messages.parse(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": content}], output_format=output_model,
            )
        except ValidationError as exc:
            last_problem = f"sortie non conforme au schéma ({exc.error_count()} erreur(s))"
            continue
        usage += usage_of(model, response)
        if response.stop_reason == "refusal":
            raise StepError("Le modèle a refusé de traiter cette demande.", usage)
        if response.parsed_output is not None:
            return response.parsed_output, usage
        last_problem = f"sortie vide (stop_reason={response.stop_reason})"
    raise StepError(f"Sortie invalide après {MAX_ATTEMPTS} tentatives : {last_problem}", usage)
