import httpx

class RetryableError(Exception):
    """Transient failure (rate limit, timeout, 5xx). Safe to retry.
    Must NOT mark the message as unprocessed and must NOT escalate"""