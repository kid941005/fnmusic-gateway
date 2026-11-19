# -*- coding: utf-8 -*-
"""结构化错误：HTTP / MCP 共用，不再把任意异常文本当协议。"""


class GatewayError(Exception):
    """带稳定错误码的网关异常。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}

    def payload(self):
        out = {"error": str(self), "code": self.code}
        if self.details:
            out["details"] = self.details
        return out


class BadRequest(GatewayError):
    code = "bad_request"
    http_status = 400


class NotFound(GatewayError):
    code = "not_found"
    http_status = 404


class UpstreamError(GatewayError):
    code = "upstream_error"
    http_status = 502


class NoSourceError(GatewayError):
    code = "no_source"
    http_status = 503


class StorageError(GatewayError):
    code = "storage_error"
    http_status = 500


def as_payload(exc):
    if isinstance(exc, GatewayError):
        return exc.payload(), exc.http_status
    return {"error": str(exc), "code": "internal_error"}, 500
