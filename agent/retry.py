from .errors import RetryableError, httpx

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

def is_retryable(exc: Exception) -> bool:
    # 1. httpx-style errors
    if isinstance(exc, (httpx.TimeoutException,
                        httpx.ConnectError,
                        httpx.TransportError)):
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in RETRYABLE_STATUSES:
        return True
    # 2. openai-SDK style: exception carries .status_code directly
    if getattr(exc, "status_code", None) in RETRYABLE_STATUSES:
        return True
    text = str(exc).lower()
    return "429" in text or "rate_limit" in text or "timeout" in text