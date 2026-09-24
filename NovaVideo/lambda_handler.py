import base64
import io
import urllib.parse
from app import app

BINARY_MIME_PREFIXES = (
    "video/",
    "audio/",
    "image/",
    "application/octet-stream",
    "application/zip",
    "application/x-zip-compressed",
)


def is_binary_content(content_type: str) -> bool:
    """Check if content type should be base64-encoded for Lambda responses."""
    lower_type = content_type.lower()
    return any(lower_type.startswith(prefix) for prefix in BINARY_MIME_PREFIXES)


def make_environ(event: dict) -> dict:
    """Transform AWS Lambda event (API Gateway v1/v2, Function URL, ALB) into a standard WSGI environ."""
    # Check event version
    version = event.get("version", "1.0")

    if version == "2.0":
        # API Gateway HTTP API or Lambda Function URL (Format 2.0)
        http_ctx = event.get("requestContext", {}).get("http", {})
        method = http_ctx.get("method", "GET")
        path = event.get("rawPath", "/")
        query_string = event.get("rawQueryString", "")
        headers = event.get("headers", {})
        cookies = event.get("cookies", [])
        if cookies and "cookie" not in headers:
            headers["cookie"] = "; ".join(cookies)
        remote_addr = http_ctx.get("sourceIp", "127.0.0.1")
    else:
        # API Gateway REST API (Format 1.0) or Application Load Balancer
        method = event.get("httpMethod", "GET")
        path = event.get("path", "/")
        query_params = event.get("queryStringParameters") or {}
        query_string = urllib.parse.urlencode(query_params)
        headers = event.get("headers") or {}
        remote_addr = (
            event.get("requestContext", {}).get("identity", {}).get("sourceIp")
            or "127.0.0.1"
        )

    # Process request body
    body_bytes = b""
    raw_body = event.get("body")
    if raw_body:
        if event.get("isBase64Encoded", False):
            body_bytes = base64.b64decode(raw_body)
        else:
            body_bytes = raw_body.encode("utf-8")

    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": remote_addr,
        "SERVER_NAME": headers.get("host", "lambda.internal").split(":")[0],
        "SERVER_PORT": "443",
        "CONTENT_LENGTH": str(len(body_bytes)),
        "CONTENT_TYPE": headers.get("content-type", ""),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": headers.get("x-forwarded-proto", "https"),
        "wsgi.input": io.BytesIO(body_bytes),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }

    # Populate HTTP_* headers
    for key, value in headers.items():
        key_upper = key.upper().replace("-", "_")
        if key_upper not in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            environ[f"HTTP_{key_upper}"] = str(value)

    return environ


def handler(event, context):
    """
    AWS Lambda entrypoint.
    Executes the Flask WSGI app and formats the response for API Gateway or Function URLs.
    """
    environ = make_environ(event)

    response_status = "200 OK"
    response_headers = []

    def start_response(status, headers, exc_info=None):
        nonlocal response_status, response_headers
        response_status = status
        response_headers = headers

    response_body_iter = app(environ, start_response)
    try:
        response_body = b"".join(response_body_iter)
    finally:
        if hasattr(response_body_iter, "close"):
            response_body_iter.close()

    status_code = int(response_status.split(" ", 1)[0])
    content_type = ""
    cookies = []
    headers_dict = {}

    for header_name, header_value in response_headers:
        name_lower = header_name.lower()
        if name_lower == "content-type":
            content_type = header_value
            headers_dict[header_name] = header_value
        elif name_lower == "set-cookie":
            cookies.append(header_value)
        else:
            headers_dict[header_name] = header_value

    is_binary = is_binary_content(content_type)
    if is_binary:
        encoded_body = base64.b64encode(response_body).decode("ascii")
    else:
        try:
            encoded_body = response_body.decode("utf-8")
        except UnicodeDecodeError:
            encoded_body = base64.b64encode(response_body).decode("ascii")
            is_binary = True

    # Output Format 2.0 (HTTP API / Function URL)
    if event.get("version") == "2.0":
        response = {
            "statusCode": status_code,
            "headers": headers_dict,
            "body": encoded_body,
            "isBase64Encoded": is_binary,
        }
        if cookies:
            response["cookies"] = cookies
        return response

    # Output Format 1.0 (REST API / ALB)
    response = {
        "statusCode": status_code,
        "headers": headers_dict,
        "body": encoded_body,
        "isBase64Encoded": is_binary,
    }
    if cookies:
        response["multiValueHeaders"] = {"Set-Cookie": cookies}

    return response
