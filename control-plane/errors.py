# SPDX-License-Identifier: AGPL-3.0-only
"""ApiError, the one error type every layer raises.

Kept in its own leaf module so store/dockerd/lifecycle/etc. can raise it without
importing the FastAPI app (which would create an import cycle).
"""


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)
