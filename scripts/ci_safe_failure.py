"""What a CI script may say about a failure (#1988).

The text of an exception can carry anything the environment holds: a command
line, a response line, a path, a header. The CI scripts that talk to the hub
write into a log everyone who can read the run can read, and into artifacts and
reports that travel onward. They say ``safe_failure(exc)`` and nothing else
about an exception; tests/test_ci_second_run_report.py walks the three scripts
and refuses a raw exception in a log, a print or a reason.

One shared module on purpose: three copies of the rule drift (this is the fourth
round of the same defect).
"""

from __future__ import annotations

import http.client
import urllib.error

# Everything a network call to the hub or to GitHub may raise. A handler that
# lists fewer lets BadStatusLine and its siblings through with the response line.
NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
    TimeoutError,
)


def safe_failure(exc: BaseException) -> str:
    """The type of ``exc``, with the code for an HTTP refusal; never its text.

    ``safe_message`` is set only by a class of ours whose message is written by
    us from fixed words (ci_trusted_report.Refused).
    """
    own = getattr(exc, "safe_message", None)
    if isinstance(own, str):
        return own
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError {exc.code}"
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.URLError) and isinstance(reason, BaseException):
        return f"URLError ({type(reason).__name__})"
    return type(exc).__name__
