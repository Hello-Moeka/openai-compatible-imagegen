#!/usr/bin/env python3
"""OpenAI-compatible Images API client used by the image-generation skill."""
from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
import warnings

MAX_RESPONSE_BYTES = 100 * 1024 * 1024
MAX_INPUT_BYTES = 50_000_000
MAX_MASK_BYTES = 4_000_000
MAX_IMAGE_REDIRECTS = 5
MAX_INPUT_IMAGES = 16
MAX_OUTPUT_IMAGES = 10
# Kept as a compatibility alias for callers that imported the old constant.
MAX_IMAGES = MAX_OUTPUT_IMAGES
AUTO_MODEL = "auto"
# Live capability checks recorded in references/provider-capability-matrix.md.
# These flags describe endpoint/parameter combinations, not model-list metadata.
VERIFIED_MODEL_CAPABILITIES = {
    "gpt-image-2.5-flare": {
        "generation": True, "edit": True, "mask": True,
        "transparent_generation": False, "transparent_edit": True,
        "batch_generation": True, "batch_edit": True, "mask_batch": True,
    },
    "gpt-image-2.5-sunburst": {
        "generation": True, "edit": True, "mask": True,
        "transparent_generation": False, "transparent_edit": True,
        "batch_generation": True, "batch_edit": True, "mask_batch": True,
    },
    "gpt-image-2": {
        "generation": True, "edit": True, "mask": True,
        "transparent_generation": False, "transparent_edit": True,
        "batch_generation": True, "batch_edit": True, "mask_batch": True,
    },
}
SUPPORTED_INPUT_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
FORMAT_BY_MIME = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}
RESERVED_EXTRA_FIELDS = {
    "model", "prompt", "n", "size", "quality", "background", "image", "mask"
}
SENSITIVE_KEY_FRAGMENTS = {
    "apikey",
    "authorization",
    "accesstoken",
    "cookie",
    "credential",
    "header",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not follow provider-controlled redirects with an authenticated request."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirectHandler())


def open_without_redirect(request: urllib.request.Request, timeout: int | None = None) -> Any:
    if timeout is None:
        return NO_REDIRECT_OPENER.open(request)
    return NO_REDIRECT_OPENER.open(request, timeout=timeout)


def load_local_env() -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    path = Path(__file__).resolve().parents[1] / ".env.local"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def configured_base_url() -> str:
    value = os.environ.get("IMAGE_API_BASE_URL", "").strip()
    if not value:
        raise ValueError("Missing IMAGE_API_BASE_URL (set it in the environment or .env.local).")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("IMAGE_API_BASE_URL must be an absolute http:// or https:// URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("IMAGE_API_BASE_URL must not contain credentials, a query, or a fragment.")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ValueError("Use HTTPS for remote API endpoints; plain HTTP is allowed only for loopback services.")
    return value.rstrip("/")


def is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def endpoint_url(base: str, suffix: str) -> str:
    return f"{base}/{suffix.lstrip('/')}"


def read_json_object(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read --extra-json: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("--extra-json must contain a JSON object.")
    conflicts = RESERVED_EXTRA_FIELDS.intersection(value)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(f"--extra-json cannot override required fields: {names}.")
    validate_extra_value(value)
    return value


def validate_extra_value(value: Any, key_path: str = "--extra-json") -> None:
    """Reject credential-shaped fields before they can reach a request or manifest."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{key_path} contains a non-string object key.")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in RESERVED_EXTRA_FIELDS:
                raise ValueError(f"{key_path}.{key} is a reserved request field.")
            if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS):
                raise ValueError(f"{key_path}.{key} is not allowed; keep credentials in IMAGE_API_KEY.")
            validate_extra_value(child, f"{key_path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            validate_extra_value(child, f"{key_path}[{index}]")
        return
    if isinstance(value, str):
        api_key = os.environ.get("IMAGE_API_KEY", "").strip()
        if (api_key and api_key in value) or any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS):
            raise ValueError(f"{key_path} contains a credential-shaped value; keep credentials in IMAGE_API_KEY.")


def validate_api_key() -> str:
    key = os.environ.get("IMAGE_API_KEY", "").strip()
    if not key:
        raise ValueError("Missing IMAGE_API_KEY (set it in the environment or .env.local).")
    if any(ord(character) < 32 for character in key):
        raise ValueError("IMAGE_API_KEY contains a control character.")
    return key


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or edit images with an OpenAI-compatible API.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="Validate local settings without making a network request.")
    mode.add_argument("--check-connection", action="store_true", help="Make an authenticated, read-only GET /models request.")
    parser.add_argument(
        "--mode",
        choices=["auto", "generate", "edit"],
        default="auto",
        help="auto selects edit mode when input images or a mask are provided.",
    )
    parser.add_argument("--operation", choices=["generations", "edits"], help=argparse.SUPPRESS)
    parser.add_argument("--prompt", help="Image prompt; required for generation and edit requests.")
    parser.add_argument(
        "--model",
        help="Model ID or auto; defaults to IMAGE_MODEL or automatic selection.",
    )
    parser.add_argument(
        "--priority",
        choices=["auto", "speed", "quality", "fidelity"],
        default="auto",
        help="Automatic model selection preference.",
    )
    parser.add_argument("--size")
    parser.add_argument("--quality")
    parser.add_argument("--background")
    parser.add_argument("--format", choices=["png", "jpeg", "webp"], dest="output_format")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument(
        "--input",
        "--image",
        action="append",
        default=[],
        dest="input",
        help="Input or reference image for edits; may be repeated.",
    )
    parser.add_argument(
        "--image-role",
        action="append",
        default=[],
        help="Role for each input image, such as identity or composition reference.",
    )
    parser.add_argument("--mask", help="Optional mask image for edits.")
    parser.add_argument("--output-dir", help="Directory for returned images and image-manifest.json.")
    parser.add_argument("--dry-run", action="store_true", help="Print a sanitized request preview without contacting the provider.")
    parser.add_argument("--extra-json", help="Path to a JSON object containing additional provider parameters.")
    parser.add_argument("--timeout", type=int, default=1800, help="Request timeout in seconds; 0 disables it.")
    return parser


def resolve_operation(args: argparse.Namespace) -> str:
    """Match api-image routing: inputs or masks imply the edit endpoint."""
    if args.operation:
        return args.operation
    if args.mode == "edit" or (args.mode == "auto" and (args.input or args.mask)):
        return "edits"
    return "generations"


def validate_model_id(model: str) -> str:
    value = model.strip()
    if not value or len(value) > 200 or any(ord(character) < 32 for character in value):
        raise ValueError("Model ID must be a non-empty string of at most 200 printable characters.")
    return value


def automatic_model_order(operation: str, *, has_mask: bool, priority: str) -> tuple[str, ...]:
    """Prefer the quality model by default; use Flare first for speed requests."""
    if priority == "speed":
        return ("gpt-image-2.5-flare", "gpt-image-2", "gpt-image-2.5-sunburst")
    # OpenAI positions Sunburst as highest quality and Flare as quality-comparable
    # to GPT Image 2; prefer Flare over GPT Image 2 as the faster tie-break.
    return ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2")


def select_auto_model(
    operation: str,
    *,
    has_mask: bool,
    priority: str = "auto",
    available_models: list[str] | None = None,
    background: str | None = None,
    output_count: int = 1,
) -> tuple[str, str]:
    """Choose a model using live availability and verified capabilities."""
    available = set(available_models) if available_models is not None else set(VERIFIED_MODEL_CAPABILITIES)
    capability = "mask" if has_mask else "edit" if operation == "edits" else "generation"
    requirements = [capability]
    if background == "transparent":
        requirements.append("transparent_edit" if operation == "edits" else "transparent_generation")
    if output_count > 1:
        if has_mask:
            requirements.append("mask_batch")
        else:
            requirements.append("batch_edit" if operation == "edits" else "batch_generation")
    for model in automatic_model_order(operation, has_mask=has_mask, priority=priority):
        if model not in available:
            continue
        model_capabilities = VERIFIED_MODEL_CAPABILITIES.get(model, {})
        if not all(model_capabilities.get(required, False) for required in requirements):
            continue
        preference = "speed-first" if priority == "speed" else "quality-first"
        reason = f"{preference} selection among available models satisfying [{', '.join(requirements)}]"
        return model, reason
    available_label = ", ".join(sorted(available)) if available else "none"
    requirement_label = ", ".join(requirements)
    raise ValueError(
        "Automatic model selection found no available model with verified "
        f"capabilities [{requirement_label}] (provider models: {available_label}). "
        "Pass --model explicitly to probe an unverified capability."
    )


def apply_image_roles(prompt: str, roles: list[str], image_count: int) -> str:
    if not roles:
        return prompt
    if image_count == 0:
        raise ValueError("--image-role requires at least one --input/--image.")
    if len(roles) > image_count:
        raise ValueError("--image-role count must not exceed the number of input images.")
    lines = ["", "Input image roles:"]
    for index, role in enumerate(roles, start=1):
        lines.append(f"Image {index}: {role}")
    return prompt + "\n" + "\n".join(lines)


def image_dimensions(data: bytes, mime_type: str) -> tuple[int, int] | None:
    """Read dimensions from common raster headers without third-party packages."""
    if mime_type == "image/png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")

    if mime_type == "image/gif" and len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")

    if mime_type == "image/jpeg" and data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            segment_length = int.from_bytes(data[offset:offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if segment_length < 7:
                    break
                height = int.from_bytes(data[offset + 3:offset + 5], "big")
                width = int.from_bytes(data[offset + 5:offset + 7], "big")
                return width, height
            offset += segment_length

    if mime_type == "image/webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return width, height
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            b1, b2, b3, b4 = data[21:25]
            width = 1 + b1 + ((b2 & 0x3F) << 8)
            height = 1 + (b2 >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
            return width, height
    return None


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def validate_local_image(
    filename: str,
    *,
    role: str,
    require_alpha: bool = False,
    required_mime_type: str | None = None,
    max_bytes: int = MAX_INPUT_BYTES,
) -> dict[str, Any]:
    """Decode an input image before it can be included in a paid request."""
    path = Path(filename).expanduser()
    if not path.is_file():
        raise ValueError(f"{role} file does not exist: {path}")
    file_size = path.stat().st_size
    if file_size >= max_bytes:
        limit_label = "50 MB" if max_bytes == MAX_INPUT_BYTES else "4 MB" if max_bytes == MAX_MASK_BYTES else f"{max_bytes} bytes"
        raise ValueError(f"{role} must be smaller than {limit_label}: {path}")
    data = path.read_bytes()
    detected = detect_image_type(data)
    if detected is None:
        raise ValueError(f"{role} is not a recognized raster image: {path}")
    _, mime_type = detected
    if mime_type not in SUPPORTED_INPUT_MIME_TYPES:
        supported = ", ".join(sorted(SUPPORTED_INPUT_MIME_TYPES))
        raise ValueError(f"{role} format {mime_type} is not supported for image edits; use {supported}.")
    if required_mime_type and mime_type != required_mime_type:
        raise ValueError(f"{role} must be {required_mime_type}, got {mime_type}.")
    expected_suffixes = {".jpg", ".jpeg"} if mime_type == "image/jpeg" else {".png"} if mime_type == "image/png" else {".webp"}
    if path.suffix.lower() not in expected_suffixes:
        raise ValueError(f"{role} extension does not match its decoded format: {path}")

    try:
        from PIL import Image
    except ImportError:
        raise ValueError("Pillow is required to validate input images before an edit request.") from None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(BytesIO(data)) as image:
                if image.format != FORMAT_BY_MIME[mime_type]:
                    raise ValueError("image signature and decoded format do not match")
                dimensions = image.size
                bands = image.getbands()
                has_alpha = "A" in bands or "transparency" in image.info
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.size != dimensions:
                    raise ValueError("image dimensions changed while decoding")
    except Exception as exc:
        raise ValueError(f"{role} cannot be fully decoded: {exc}") from None

    if dimensions[0] < 1 or dimensions[1] < 1:
        raise ValueError(f"{role} has invalid dimensions: {path}")
    if require_alpha and not has_alpha:
        raise ValueError(f"{role} must contain an alpha channel: {path}")
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "bytes": file_size,
        "mime_type": mime_type,
        "width": dimensions[0],
        "height": dimensions[1],
        "has_alpha": has_alpha,
    }


def validate_image_bytes(
    data: bytes,
    role: str,
    *,
    require_transparency: bool = False,
) -> tuple[str, str, tuple[int, int]]:
    detected = detect_image_type(data)
    if detected is None:
        raise ValueError(f"{role} is not a recognized raster image.")
    extension, mime_type = detected
    try:
        from PIL import Image
    except ImportError:
        raise ValueError("Pillow is required to verify provider image responses before saving them.") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(BytesIO(data)) as image:
                if image.format != FORMAT_BY_MIME.get(mime_type, image.format):
                    raise ValueError("image signature and decoded format do not match")
                dimensions = image.size
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.size != dimensions:
                    raise ValueError("image dimensions changed while decoding")
                if require_transparency:
                    if "A" not in image.getbands() and "transparency" not in image.info:
                        raise ValueError("transparent output has no alpha channel")
                    alpha_min, _ = image.convert("RGBA").getchannel("A").getextrema()
                    if alpha_min == 255:
                        raise ValueError("transparent output contains no transparent pixels")
    except Exception as exc:
        raise ValueError(f"{role} cannot be fully decoded: {exc}") from None
    if dimensions[0] < 1 or dimensions[1] < 1:
        raise ValueError(f"{role} has invalid dimensions.")
    return extension, mime_type, dimensions


def validate_edit_inputs(inputs: list[str], mask: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if len(inputs) > MAX_INPUT_IMAGES:
        raise ValueError(f"An edit request may include at most {MAX_INPUT_IMAGES} input images.")
    input_info = [
        validate_local_image(filename, role=f"Input image {index}")
        for index, filename in enumerate(inputs, start=1)
    ]
    mask_info: dict[str, Any] | None = None
    if mask:
        mask_info = validate_local_image(
            mask,
            role="Mask",
            require_alpha=True,
            required_mime_type="image/png",
            max_bytes=MAX_MASK_BYTES,
        )
        first = input_info[0]
        if (mask_info["width"], mask_info["height"]) != (first["width"], first["height"]):
            raise ValueError("Mask dimensions must match the first input image dimensions.")
    return input_info, mask_info


def ensure_output_dir_isolated(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.iterdir())
    if existing:
        names = ", ".join(entry.name for entry in existing[:3])
        suffix = "..." if len(existing) > 3 else ""
        raise ValueError(
            "Output directory must be empty before a request so images and image-manifest.json stay isolated; "
            f"found {names}{suffix}: {output_dir}"
        )


def parse_models_response(result: Any) -> list[str]:
    """Validate the minimum OpenAI-compatible GET /models response shape."""
    if not isinstance(result, dict) or result.get("object") != "list":
        raise ValueError("Provider /models response must be an object with object=\"list\".")
    if "success" in result and result["success"] is not True:
        raise ValueError("Provider /models response reported success=false.")
    models = result.get("data")
    if not isinstance(models, list):
        raise ValueError("Provider /models response data must be a list.")
    model_ids: list[str] = []
    for index, item in enumerate(models):
        if not isinstance(item, dict) or item.get("object") != "model" or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError(f"Provider /models response data[{index}] is not a valid model object.")
        model_ids.append(item["id"])
    return model_ids


def decode_image_item(item: dict[str, Any], index: int) -> tuple[bytes, str | None]:
    source_url = item.get("url") if isinstance(item.get("url"), str) else None
    encoded = item.get("b64_json") or item.get("b64")
    if isinstance(encoded, str):
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or ";base64" not in header.lower():
                raise ValueError(f"Provider response data[{index}] contains an unsupported image data URL.")
        try:
            return base64.b64decode(encoded, validate=True), source_url
        except (binascii.Error, ValueError):
            raise ValueError(f"Provider response data[{index}] contains invalid base64 image data.") from None
    if not source_url:
        raise ValueError(f"Provider response data[{index}] has no b64_json, b64, or url image field.")
    try:
        return download_provider_image(source_url), source_url
    except ValueError as exc:
        raise ValueError(f"Provider response data[{index}]: {exc}") from None


def image_item_response_type(item: dict[str, Any]) -> str:
    """Identify the response transport without retaining provider URLs."""
    encoded = item.get("b64_json") or item.get("b64")
    if isinstance(encoded, str):
        if encoded.startswith("data:"):
            return "data_url"
        return "b64_json" if isinstance(item.get("b64_json"), str) else "b64"
    if isinstance(item.get("url"), str):
        return "url"
    return "unknown"


def validate_provider_image_url(url: str) -> None:
    if len(url) > 8192:
        raise ValueError("provider image URL is too long")
    parsed_url = urllib.parse.urlsplit(url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or (parsed_url.scheme == "http" and not is_loopback_host(parsed_url.hostname))
    ):
        raise ValueError("provider image URL or redirect target is unsafe")


def download_provider_image(source_url: str) -> bytes:
    current_url = source_url
    redirects = 0
    while True:
        validate_provider_image_url(current_url)
        request = urllib.request.Request(current_url, method="GET")
        request.add_header("Accept", "image/*")
        request.add_header("User-Agent", "OpenAI-Compatible-ImageClient/1.0")
        try:
            with open_without_redirect(request, timeout=60) as remote:
                return bounded_read(remote)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ValueError(f"provider image download failed (HTTP {exc.code})") from None
            location = exc.headers.get("Location")
            exc.close()
            if not location or redirects >= MAX_IMAGE_REDIRECTS:
                raise ValueError("provider image download exceeded the redirect limit") from None
            next_url = urllib.parse.urljoin(current_url, location)
            validate_provider_image_url(next_url)
            if urllib.parse.urlsplit(current_url).scheme == "https" and urllib.parse.urlsplit(next_url).scheme != "https":
                raise ValueError("provider image redirect cannot downgrade from HTTPS")
            current_url = next_url
            redirects += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("could not download the image returned by the provider") from None


def bounded_read(response: Any) -> bytes:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError(f"Response exceeded the {MAX_RESPONSE_BYTES // (1024 * 1024)} MiB safety limit.")
    return data


def safe_error_detail(body: bytes, api_key: str | None = None) -> str:
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
        if isinstance(error, dict):
            detail = error.get("message") or error.get("type") or "Provider returned an error."
        else:
            detail = "Provider returned an error."
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = "Provider returned a non-JSON error response."
    detail = re.sub(r"[\r\n\t]+", " ", str(detail))
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    detail = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", detail)
    return detail[:400]


def fetch_model_ids(base: str, api_key: str, timeout: int = 20) -> list[str]:
    request = urllib.request.Request(endpoint_url(base, "models"), method="GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "OpenAI-Compatible-ImageClient/1.0")
    try:
        with open_without_redirect(request, timeout=timeout) as response:
            result = json.loads(bounded_read(response).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = safe_error_detail(exc.read(8192), api_key)
        raise ValueError(f"model discovery failed: HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, OSError) and getattr(reason, "errno", None) in {11001, 11002, -2, -3}:
            detail = "DNS lookup failed. The current execution sandbox may block external network access."
        elif isinstance(reason, TimeoutError):
            detail = "Connection timed out. Check provider availability and this execution environment's network access."
        else:
            detail = f"Network request failed: {reason}"
        raise ValueError(f"model discovery failed: {detail}") from None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"model discovery failed: invalid provider response ({exc})") from None

    try:
        return parse_models_response(result)
    except ValueError as exc:
        raise ValueError(f"model discovery failed: invalid provider response ({exc})") from None


def connection_check(base: str, api_key: str) -> int:
    try:
        model_ids = fetch_model_ids(base, api_key)
    except ValueError as exc:
        print(f"Connection check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "http_status": 200, "model_count": len(model_ids), "models": model_ids}, ensure_ascii=False, indent=2))
    return 0


def write_image(
    data: bytes,
    output_dir: Path,
    index: int,
    source_url: str | None = None,
    response_type: str = "unknown",
    require_transparency: bool = False,
) -> dict[str, Any]:
    extension, mime_type, dimensions = validate_image_bytes(
        data,
        f"Response item {index}",
        require_transparency=require_transparency,
    )
    base_name = f"image-{index}{extension}"
    target = output_dir / base_name
    suffix = 2
    while target.exists():
        target = output_dir / f"image-{index}-{suffix}{extension}"
        suffix += 1
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(target),
        "bytes": len(data),
        "mime_type": mime_type,
        "width": dimensions[0] if dimensions else None,
        "height": dimensions[1] if dimensions else None,
        "response_type": response_type,
    }


def request_image(
    base: str,
    api_key: str,
    operation: str,
    payload: dict[str, Any],
    inputs: list[str],
    mask: str | None,
    output_dir: Path,
    timeout: int | None,
    model_selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    boundary = "----CodexImage" + secrets.token_hex(16)
    if operation == "generations":
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"
    else:
        parts: list[bytes] = []

        def field(name: str, value: Any) -> None:
            if isinstance(value, (dict, list, bool)) or value is None:
                text = json.dumps(value, ensure_ascii=False)
            else:
                text = str(value)
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{text}\r\n".encode("utf-8"))

        def file_part(name: str, filename: str) -> None:
            path = Path(filename)
            if not path.is_file():
                raise ValueError(f"Input file does not exist: {path}")
            if path.stat().st_size >= MAX_INPUT_BYTES:
                raise ValueError(f"Input file must be smaller than 50 MB: {path}")
            safe_filename = path.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            parts.extend([
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{safe_filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode("utf-8"),
                path.read_bytes(),
                b"\r\n",
            ])

        for key, value in payload.items():
            field(key, value)
        for filename in inputs:
            file_part("image[]", filename)
        if mask:
            file_part("mask", mask)
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"

    request = urllib.request.Request(endpoint_url(base, f"images/{operation}"), data=body, method="POST")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", content_type)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "OpenAI-Compatible-ImageClient/1.0")
    try:
        response_context = open_without_redirect(request, timeout=timeout)
        with response_context as response:
            result = json.loads(bounded_read(response).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = safe_error_detail(exc.read(8192), api_key)
        raise ValueError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, OSError) and getattr(reason, "errno", None) in {11001, 11002, -2, -3}:
            raise ValueError("DNS lookup failed. The current execution sandbox may block external network access.") from None
        if isinstance(reason, TimeoutError):
            raise ValueError("Request timed out. Check provider availability and this execution environment's network access.") from None
        raise ValueError(f"Network request failed: {reason}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Provider returned an invalid JSON response.") from None

    items = result.get("data") if isinstance(result, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("Provider response did not contain any image data in data[].")
    requested_count = payload.get("n")
    if isinstance(requested_count, int) and len(items) != requested_count:
        raise ValueError(f"Provider returned {len(items)} image(s), but the request asked for {requested_count}.")

    outputs: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError(f"Provider response data[{index - 1}] is not an object.")
            response_type = image_item_response_type(item)
            image_data, source_url = decode_image_item(item, index - 1)
            outputs.append(
                write_image(
                    image_data,
                    output_dir,
                    index,
                    source_url,
                    response_type,
                    require_transparency=payload.get("background") == "transparent",
                )
            )
    except Exception:
        for output in outputs:
            Path(output["path"]).unlink(missing_ok=True)
        raise

    manifest = {
        "model": payload["model"],
        "operation": operation,
        "provider": urllib.parse.urlsplit(base).hostname,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request_parameters": {
            key: value
            for key, value in payload.items()
            if key not in {"prompt", *extra_parameter_names(payload)}
        },
        "extra_parameter_names": sorted(extra_parameter_names(payload)),
        "model_selection": model_selection,
        "input_files": [Path(filename).name for filename in inputs],
        "mask_file": Path(mask).name if mask else None,
        "outputs": outputs,
    }
    manifest_path = output_dir / "image-manifest.json"
    temporary_manifest = output_dir / f".image-manifest.{secrets.token_hex(4)}.tmp"
    try:
        temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_manifest.replace(manifest_path)
        return manifest
    except Exception:
        for output in outputs:
            Path(output["path"]).unlink(missing_ok=True)
        raise
    finally:
        temporary_manifest.unlink(missing_ok=True)


def extra_parameter_names(payload: dict[str, Any]) -> set[str]:
    return set(payload).difference({"model", "prompt", "n", "size", "quality", "background", "output_format"})


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        base = configured_base_url()
        configured_model = args.model if args.model is not None else os.environ.get("IMAGE_MODEL", AUTO_MODEL)
        configured_model = configured_model.strip() or AUTO_MODEL
        model_setting = configured_model if configured_model.lower() == AUTO_MODEL else validate_model_id(configured_model)

        if args.check_config:
            validate_api_key()
            print(json.dumps({"ok": True, "provider": urllib.parse.urlsplit(base).hostname, "model": model_setting, "api_key_configured": True}, indent=2))
            return 0
        if args.check_connection:
            return connection_check(base, validate_api_key())

        if not args.prompt:
            parser.error("--prompt is required unless --check-config or --check-connection is used.")
        if args.timeout < 0:
            raise ValueError("--timeout must be 0 or a positive integer.")
        operation = resolve_operation(args)
        prompt = apply_image_roles(args.prompt, args.image_role, len(args.input))
        if args.n < 1 or args.n > MAX_OUTPUT_IMAGES:
            raise ValueError(f"--n must be between 1 and {MAX_OUTPUT_IMAGES}.")
        if operation == "edits" and not args.input:
            raise ValueError("--input is required for edits.")
        if operation == "generations" and (args.input or args.mask):
            raise ValueError("--input and --mask are only valid for edits.")
        if operation == "edits":
            validate_edit_inputs(args.input, args.mask)
        extra_parameters = read_json_object(args.extra_json)
        output_format = args.output_format
        if output_format and "output_format" in extra_parameters:
            raise ValueError("--format cannot be combined with --extra-json output_format.")
        if "output_format" in extra_parameters:
            output_format = extra_parameters["output_format"]
        if output_format is not None and output_format not in {"png", "jpeg", "webp"}:
            raise ValueError("output_format must be png, jpeg, or webp.")
        if args.background == "transparent":
            if output_format is None:
                output_format = "png"
            if output_format == "jpeg":
                raise ValueError("Transparent background requires --format png or --format webp.")

        output_dir: Path | None = None
        if not args.dry_run:
            if not args.output_dir:
                raise ValueError("--output-dir is required unless --dry-run or a check mode is used.")
            output_dir = Path(args.output_dir).expanduser().resolve()
            ensure_output_dir_isolated(output_dir)

        api_key: str | None = None
        if model_setting.lower() == AUTO_MODEL:
            if args.dry_run:
                available_models = None
                selection_source = "local verified capability matrix"
            else:
                api_key = validate_api_key()
                available_models = fetch_model_ids(base, api_key)
                selection_source = "provider /models plus local verified capability matrix"
            model, selection_reason = select_auto_model(
                operation,
                has_mask=bool(args.mask),
                priority=args.priority,
                available_models=available_models,
                background=args.background,
                output_count=args.n,
            )
            model_selection = {
                "mode": "auto",
                "priority": args.priority,
                "reason": selection_reason,
                "source": selection_source,
            }
        else:
            model = model_setting
            model_selection = {
                "mode": "explicit" if args.model is not None else "configured",
                "priority": args.priority,
                "reason": "model was supplied by the caller configuration",
            }

        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": args.n}
        for key in ("size", "quality", "background"):
            value = getattr(args, key)
            if value:
                payload[key] = value
        if output_format:
            payload["output_format"] = output_format
        payload.update(extra_parameters)
        preview = {
            "url": endpoint_url(base, f"images/{operation}"),
            "operation": operation,
            "model": model,
            "model_selection": model_selection,
            "parameters": {key: value for key, value in payload.items() if key not in {"prompt", *extra_parameters}},
            "extra_parameter_names": sorted(extra_parameters),
            "prompt_chars": len(prompt),
            "input_files": args.input,
            "image_roles": args.image_role,
            "mask_file": args.mask,
            "timeout_seconds": args.timeout or None,
        }
        if args.dry_run:
            print(json.dumps(preview, ensure_ascii=False, indent=2))
            return 0

        if api_key is None:
            api_key = validate_api_key()
        assert output_dir is not None
        manifest = request_image(
            base,
            api_key,
            operation,
            payload,
            args.input,
            args.mask,
            output_dir,
            args.timeout or None,
            model_selection,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
