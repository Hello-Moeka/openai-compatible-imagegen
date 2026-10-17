#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Hello-Moeka
"""Check saved raster images and their image-generation manifest."""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from io import BytesIO
from pathlib import Path
from typing import Any

from image_api import detect_image_type, image_dimensions

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 100 * 1024 * 1024
FORMAT_BY_MIME = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
SENSITIVE_MANIFEST_KEYS = {
    "prompt",
    "apikey",
    "authorization",
    "accesstoken",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}


def verify_image(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB inspection limit")
    data = path.read_bytes()
    detected = detect_image_type(data)
    if detected is None:
        raise ValueError("unrecognized or invalid image signature")
    _, mime_type = detected
    dimensions = image_dimensions(data, mime_type)
    validation = "signature"

    try:
        from PIL import Image
    except ImportError:
        Image = None

    if Image is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with Image.open(BytesIO(data)) as image:
                if image.format != FORMAT_BY_MIME[mime_type]:
                    raise ValueError("image signature and decoded format do not match")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image.load()
                width, height = image.size
        dimensions = (width, height)
        validation = "decoded"
    else:
        if dimensions is None:
            raise ValueError("dimensions could not be read without Pillow")
        if mime_type == "image/png" and b"IEND\xaeB`\x82" not in data[-16:]:
            raise ValueError("PNG is missing a complete IEND chunk")
        if mime_type == "image/jpeg" and not data.endswith(b"\xff\xd9"):
            raise ValueError("JPEG is missing its end marker")
        if mime_type == "image/gif" and not data.endswith(b"\x3b"):
            raise ValueError("GIF is missing its trailer")
        if mime_type == "image/webp":
            declared_size = int.from_bytes(data[4:8], "little") + 8
            if declared_size > len(data):
                raise ValueError("WebP file is truncated")

    if not dimensions or dimensions[0] < 1 or dimensions[1] < 1:
        raise ValueError("image dimensions are missing or invalid")
    expected_suffix = ".jpg" if mime_type == "image/jpeg" else ".webp" if mime_type == "image/webp" else ".gif" if mime_type == "image/gif" else ".png"
    if path.suffix.lower() not in ({".jpg", ".jpeg"} if expected_suffix == ".jpg" else {expected_suffix}):
        raise ValueError(f"file extension does not match {mime_type}")

    return {
        "path": str(path.resolve()),
        "bytes": len(data),
        "mime_type": mime_type,
        "width": dimensions[0],
        "height": dimensions[1],
        "validation": validation,
    }


def read_manifest(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_path = path / "image-manifest.json"
    if not manifest_path.exists():
        return None, ["image-manifest.json is missing"]
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None, ["image-manifest.json cannot be read as valid JSON"]
    if not isinstance(value, dict):
        return None, ["image-manifest.json must contain a JSON object"]
    outputs = value.get("outputs")
    if not isinstance(outputs, list):
        return value, ["manifest outputs must be a list"]
    if not outputs:
        return value, ["manifest outputs must not be empty"]
    sensitive_fields = find_sensitive_manifest_fields(value)
    if sensitive_fields:
        return value, ["manifest contains disallowed fields: " + ", ".join(sensitive_fields[:5])]
    return value, []


def find_sensitive_manifest_fields(value: Any, key_path: str = "") -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, child in value.items():
            normalized = "".join(character for character in key.lower() if character.isalnum())
            child_path = f"{key_path}.{key}" if key_path else key
            if normalized in SENSITIVE_MANIFEST_KEYS or any(
                marker in normalized for marker in ("apikey", "authorization", "credential", "password", "secret", "token")
            ):
                found.append(child_path)
            found.extend(find_sensitive_manifest_fields(child, child_path))
        return found
    if isinstance(value, list):
        return [found for item in value for found in find_sensitive_manifest_fields(item, key_path)]
    return []


def output_path(item: Any, root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    if isinstance(item, str):
        return (Path(item) if Path(item).is_absolute() else root / item), None
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        return None, None
    value = item["path"]
    return (Path(value) if Path(value).is_absolute() else root / value), item


def inspect_directory(directory: Path) -> tuple[dict[str, Any], bool]:
    errors: list[str] = []
    manifest_errors: list[str] = []
    manifest, manifest_errors = read_manifest(directory)
    errors.extend(manifest_errors)

    images: list[dict[str, Any]] = []
    for file_path in sorted(directory.iterdir()):
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            images.append(verify_image(file_path))
        except Exception as exc:
            errors.append(f"{file_path.name}: {type(exc).__name__}: {exc}")

    checked_manifest_outputs = 0
    manifest_output_targets: set[Path] = set()
    if manifest is not None and isinstance(manifest.get("outputs"), list):
        for index, item in enumerate(manifest["outputs"], 1):
            target, metadata = output_path(item, directory)
            if target is None:
                errors.append(f"manifest outputs[{index - 1}] has no valid path")
                manifest_errors.append(f"outputs[{index - 1}] has no valid path")
                continue
            checked_manifest_outputs += 1
            resolved_target = target.resolve()
            try:
                resolved_target.relative_to(directory.resolve())
            except ValueError:
                errors.append(f"manifest output {target.name} is outside the inspected directory")
                manifest_errors.append(f"outputs[{index - 1}] points outside the inspected directory")
                continue
            if resolved_target in manifest_output_targets:
                errors.append(f"manifest output {target.name} is listed more than once")
                manifest_errors.append(f"outputs[{index - 1}] is duplicated")
                continue
            manifest_output_targets.add(resolved_target)
            if not resolved_target.is_file():
                errors.append(f"manifest output {target.name} is missing")
                manifest_errors.append(f"outputs[{index - 1}] is missing")
                continue
            actual = next((image for image in images if Path(image["path"]) == resolved_target), None)
            if actual is None:
                errors.append(f"manifest output {target.name} is not a valid scanned image")
                manifest_errors.append(f"outputs[{index - 1}] is not a valid scanned image")
                continue
            if metadata:
                for key in ("bytes", "mime_type", "width", "height"):
                    if metadata.get(key) is not None and metadata[key] != actual[key]:
                        errors.append(f"manifest output {target.name} has mismatched {key}")
                        manifest_errors.append(f"outputs[{index - 1}] has mismatched {key}")

    if manifest is not None and isinstance(manifest.get("outputs"), list):
        scanned_paths = {Path(image["path"]) for image in images}
        unlisted = sorted(scanned_paths - manifest_output_targets)
        for path in unlisted:
            errors.append(f"image {path.name} is not listed in image-manifest.json")

    if not images:
        errors.append("no valid image files found")
    result = {
        "directory": str(directory.resolve()),
        "manifest": "valid" if manifest is not None and not manifest_errors else "invalid",
        "manifest_outputs_checked": checked_manifest_outputs,
        "unlisted_images": [str(path) for path in sorted({Path(image["path"]) for image in images} - manifest_output_targets)] if manifest is not None else [],
        "image_count": len(images),
        "images": images,
        "errors": errors,
        "ok": not errors,
    }
    return result, not errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect saved images and their manifest.")
    parser.add_argument("directory", type=Path, help="Directory containing images and image-manifest.json")
    args = parser.parse_args(argv)
    directory = args.directory.expanduser()
    if not directory.is_dir():
        print(json.dumps({"ok": False, "errors": ["directory does not exist"]}, indent=2))
        return 1
    result, ok = inspect_directory(directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
