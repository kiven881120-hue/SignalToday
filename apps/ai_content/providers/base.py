from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    error_code = "PROVIDER_ERROR"
    retryable = False


class PermanentProviderError(ProviderError):
    error_code = "PERMANENT_ERROR"


class TransientProviderError(ProviderError):
    error_code = "TRANSIENT_ERROR"
    retryable = True


class ProviderTimeout(TransientProviderError):
    error_code = "TIMEOUT"


class ProviderRateLimited(TransientProviderError):
    error_code = "RATE_LIMITED"


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int


class LLMProvider(ABC):
    @abstractmethod
    def analyze_structured(
        self, *, model, system_prompt, user_prompt, schema, timeout, max_output_tokens
    ) -> ProviderResponse:
        raise NotImplementedError
