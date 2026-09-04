"""Deciding whether a failure is worth retrying elsewhere (Layer 3.5).

The rule from DECISIONS.md, and it is the whole point of this module:

    Trigger on throttling, timeouts and 5xx only. Never on schema or prompt
    errors, which fail identically on every provider and would burn three
    quotas on one bug.

So the classifier is deliberately a whitelist. An unrecognised exception is
*not* retryable: a new error type we have not thought about should surface as a
bug, not silently consume the fallback chain.
"""

from __future__ import annotations

import re
from enum import Enum


class Retryability(str, Enum):
    """What to do with a failed model call."""

    # Throttling, timeout, 5xx: the request was fine, the service was not.
    FALLBACK = "fallback"
    # Schema, prompt, validation, auth: fails identically everywhere.
    FATAL = "fatal"


# Botocore error codes that mean "the service could not serve this right now".
_RETRYABLE_AWS_CODES = frozenset({
    "ThrottlingException",
    "ThrottledException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "ProvisionedThroughputExceededException",
    "ServiceUnavailableException",
    "ServiceUnavailable",
    "InternalServerException",
    "InternalFailure",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "RequestTimeout",
    "RequestTimeoutException",
    "SlowDown",
})

# Error codes that are our own fault and will fail the same way on any provider.
_FATAL_AWS_CODES = frozenset({
    "ValidationException",
    "AccessDeniedException",
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "ResourceNotFoundException",
    "ModelNotFoundException",
    "SerializationException",
    "IncompleteSignature",
    "MissingAuthenticationToken",
    "ExpiredTokenException",
})

# Python's own data errors. These are bugs in our code or our prompt, and their
# message text must never be consulted: a schema error reading "field 'timeout'
# is required" would otherwise look retryable and burn all three quotas.
# Transport failures (TimeoutError, ConnectionError) are matched earlier, before
# this list is reached, so listing OSError here does not shadow them.
_NEVER_RETRY_TYPES = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    NotImplementedError,
    AssertionError,
)

# Last-resort textual signals, used only when there is no structured code.
_RETRYABLE_TEXT = re.compile(
    r"throttl|too many requests|rate ?limit|timed? ?out|timeout|"
    r"service unavailable|temporarily unavailable|try again|"
    r"connection (reset|aborted|refused)|remote end closed",
    re.IGNORECASE,
)


def classify(exc: BaseException) -> Retryability:
    """Decide whether `exc` justifies falling back to another model.

    Checked in order of how trustworthy the signal is: explicit AWS error codes
    first, then HTTP status, then the exception type, then finally the message
    text. Anything unrecognised is FATAL by design.
    """
    # 1. Strands' own throttling signal, raised by every model provider it wraps.
    if type(exc).__name__ == "ModelThrottledException":
        return Retryability.FALLBACK

    # 2. Structured botocore error: the most reliable signal available.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        if code in _FATAL_AWS_CODES:
            return Retryability.FATAL
        if code in _RETRYABLE_AWS_CODES:
            return Retryability.FALLBACK
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            if status == 429 or status >= 500:
                return Retryability.FALLBACK
            if 400 <= status < 500:
                # 4xx other than 429 is a malformed request. Ours to fix.
                return Retryability.FATAL

    # 3. A bare status code attribute, as some SDKs expose.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return Retryability.FALLBACK
        if 400 <= status < 500:
            return Retryability.FATAL

    # 4. Transport-level failures: the request never got an answer.
    if isinstance(exc, TimeoutError | ConnectionError):
        return Retryability.FALLBACK

    # 5. Text, only when nothing structured was available. Deliberately last:
    #    a schema error whose message happens to contain "timeout" would
    #    otherwise burn the whole chain.
    name = type(exc).__name__
    if name in _RETRYABLE_AWS_CODES or name in {
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
    }:
        return Retryability.FALLBACK
    if name in _FATAL_AWS_CODES:
        return Retryability.FATAL

    # 5. Text, only when nothing structured was available *and* the exception is
    #    not one of Python's own data errors. A schema bug whose message happens
    #    to read "field 'timeout' is required" is our fault, not the service's,
    #    and matching on its text would burn the whole chain on one bad prompt.
    if isinstance(exc, _NEVER_RETRY_TYPES):
        return Retryability.FATAL

    if _RETRYABLE_TEXT.search(str(exc)):
        return Retryability.FALLBACK

    # Unknown. Surface it rather than spending two more quotas on it.
    return Retryability.FATAL


def is_retryable(exc: BaseException) -> bool:
    """True when the failure justifies trying the next model in the chain."""
    return classify(exc) is Retryability.FALLBACK
