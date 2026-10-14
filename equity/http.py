"""Shared HTTP retry handling; never include keys or response bodies in errors."""
import time
import httpx
from .models import DataQualityError, TemporaryAPIError, PermanentAPIError, InterruptedRun


def request_json(client, method, url, **kwargs):
    try:
        response = client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise TemporaryAPIError(type(exc).__name__) from None
    if response.status_code in (408, 429) or response.status_code >= 500:
        raise TemporaryAPIError(f'HTTP {response.status_code}')
    if response.status_code in (402, 432):
        raise PermanentAPIError(f'Provider quota/usage limit reached (HTTP {response.status_code})')
    if response.status_code >= 400:
        raise PermanentAPIError(f'HTTP {response.status_code}')
    try:
        return response.json()
    except ValueError:
        raise DataQualityError('API response is not JSON') from None


def retry(operation, settings, stop=None):
    for attempt in range(settings.retries + 1):
        if stop is not None and stop.is_set():
            raise InterruptedRun()
        try:
            return operation()
        except TemporaryAPIError:
            if attempt == settings.retries:
                raise
            delay = settings.retry_base_seconds * 2 ** attempt
            if stop is None:
                time.sleep(delay)
            elif stop.wait(delay):
                raise InterruptedRun() from None
