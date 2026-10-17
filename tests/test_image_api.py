from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_ROOT / "scripts"))

import image_api  # noqa: E402
import inspect_output  # noqa: E402


def png_bytes(size: tuple[int, int] = (4, 4), mode: str = "RGBA") -> bytes:
    image = Image.new(mode, size, (255, 0, 0, 128) if "A" in mode else (255, 0, 0))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def write_png(path: Path, size: tuple[int, int] = (4, 4), mode: str = "RGBA") -> None:
    path.write_bytes(png_bytes(size, mode))


class MockHandler(BaseHTTPRequestHandler):
    response_body = b"{}"
    response_status = 200
    response_headers: dict[str, str] = {"Content-Type": "application/json"}
    requests: list[dict[str, object]] = []
    redirect_target_hit = False
    redirect_posts = False
    image_redirects = False

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/redirect"):
            type(self).requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
            self.end_headers()
            return
        if type(self).image_redirects and self.path.endswith("/image"):
            type(self).requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            self.send_response(302)
            self.send_header("Location", f"/image-final")
            self.end_headers()
            return
        if self.path.endswith("/target"):
            type(self).redirect_target_hit = True
            type(self).requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        self.send_response(type(self).response_status)
        for key, value in type(self).response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if type(self).redirect_posts and self.path.endswith("/images/generations"):
            type(self).requests.append(
                {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
            )
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
            self.end_headers()
            return
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(type(self).response_status)
        for key, value in type(self).response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def mock_server(
    response_body: bytes,
    *,
    response_status: int = 200,
    content_type: str = "application/json",
    redirect_posts: bool = False,
    image_redirects: bool = False,
):
    MockHandler.response_body = response_body
    MockHandler.response_status = response_status
    MockHandler.response_headers = {"Content-Type": content_type}
    MockHandler.requests = []
    MockHandler.redirect_target_hit = False
    MockHandler.redirect_posts = redirect_posts
    MockHandler.image_redirects = image_redirects
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class ImageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_env = os.environ.copy()
        os.environ["IMAGE_API_KEY"] = "test-key-that-must-not-leak"
        os.environ["IMAGE_API_BASE_URL"] = "https://provider.example/v1"
        os.environ["IMAGE_MODEL"] = "gpt-image-2"
        self.local_env_patch = patch.object(image_api, "load_local_env")
        self.local_env_patch.start()
        self.addCleanup(self.local_env_patch.stop)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = image_api.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_output_count_is_limited_to_ten(self) -> None:
        code, _, stderr = self.run_main(["--prompt", "test", "--n", "11", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("between 1 and 10", stderr)

    def test_auto_model_selection_uses_task_capabilities_and_priority(self) -> None:
        self.assertEqual(
            image_api.select_auto_model(
                "generations",
                has_mask=False,
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "edits",
                has_mask=False,
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "edits",
                has_mask=True,
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "edits",
                has_mask=True,
                priority="quality",
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "edits",
                has_mask=False,
                output_count=2,
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "edits",
                has_mask=False,
                output_count=2,
                priority="speed",
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-flare",
        )
        self.assertEqual(
            image_api.select_auto_model(
                "generations",
                has_mask=False,
                priority="quality",
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )[0],
            "gpt-image-2.5-sunburst",
        )

    def test_auto_model_selection_rejects_unverified_transparent_generation(self) -> None:
        with self.assertRaisesRegex(ValueError, "transparent_generation"):
            image_api.select_auto_model(
                "generations",
                has_mask=False,
                background="transparent",
                available_models=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            )

    def test_transparent_background_defaults_to_png(self) -> None:
        code, stdout, stderr = self.run_main([
            "--prompt", "test", "--model", "gpt-image-2", "--background", "transparent", "--dry-run"
        ])
        self.assertEqual(code, 0, stderr)
        preview = json.loads(stdout)
        self.assertEqual(preview["parameters"]["background"], "transparent")
        self.assertEqual(preview["parameters"]["output_format"], "png")

    def test_auto_dry_run_uses_local_matrix_without_model_discovery(self) -> None:
        with patch.dict(os.environ, {"IMAGE_MODEL": "auto"}, clear=False), patch.object(
            image_api, "fetch_model_ids", side_effect=AssertionError("dry-run must not discover models")
        ):
            code, stdout, stderr = self.run_main(["--prompt", "test", "--dry-run"])
        self.assertEqual(code, 0, stderr)
        preview = json.loads(stdout)
        self.assertEqual(preview["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(preview["model_selection"]["source"], "local verified capability matrix")

    def test_auto_live_request_discovers_models_and_records_selection(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        with mock_server(body) as server, tempfile.TemporaryDirectory() as temporary:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            output_dir = Path(temporary) / "run"
            with patch.dict(
                os.environ,
                {"IMAGE_API_BASE_URL": base, "IMAGE_MODEL": "auto"},
                clear=False,
            ), patch.object(
                image_api,
                "fetch_model_ids",
                return_value=["gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst"],
            ) as discover:
                code, stdout, stderr = self.run_main([
                    "--prompt", "test", "--output-dir", str(output_dir), "--timeout", "10"
                ])
            self.assertEqual(code, 0, stderr)
            discover.assert_called_once()
            manifest = json.loads((output_dir / "image-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["model"], "gpt-image-2.5-sunburst")
            self.assertEqual(manifest["model_selection"]["mode"], "auto")
            self.assertIn("provider /models", manifest["model_selection"]["source"])
            request = MockHandler.requests[0]
            request_payload = json.loads(request["body"])
            self.assertEqual(request_payload["model"], "gpt-image-2.5-sunburst")
            self.assertNotIn("test-key-that-must-not-leak", stdout)

    def test_edit_input_count_is_limited_to_sixteen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = []
            for index in range(17):
                path = root / f"input-{index}.png"
                write_png(path)
                inputs.extend(["--input", str(path)])
            code, _, stderr = self.run_main(["--prompt", "edit", "--mode", "edit", *inputs, "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("at most 16 input images", stderr)

    def test_invalid_input_is_rejected_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not-an-image.png"
            path.write_text("not an image", encoding="utf-8")
            code, _, stderr = self.run_main(["--prompt", "edit", "--mode", "edit", "--input", str(path), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("not a recognized raster image", stderr)

    def test_truncated_png_with_valid_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "truncated.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
            with self.assertRaisesRegex(ValueError, "cannot be fully decoded"):
                image_api.validate_local_image(str(path), role="Input image 1")

    def test_mask_requires_png_alpha_and_matching_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.png"
            no_alpha = root / "no-alpha.png"
            wrong_size = root / "wrong-size.png"
            wrong_format = root / "mask.jpg"
            write_png(input_path, (4, 4), "RGBA")
            write_png(no_alpha, (4, 4), "RGB")
            write_png(wrong_size, (2, 2), "RGBA")
            Image.new("RGB", (4, 4), "red").save(wrong_format, format="JPEG")

            code, _, stderr = self.run_main([
                "--prompt", "edit", "--mode", "edit", "--input", str(input_path), "--mask", str(no_alpha), "--dry-run"
            ])
            self.assertEqual(code, 1)
            self.assertIn("must contain an alpha channel", stderr)

            code, _, stderr = self.run_main([
                "--prompt", "edit", "--mode", "edit", "--input", str(input_path), "--mask", str(wrong_size), "--dry-run"
            ])
            self.assertEqual(code, 1)
            self.assertIn("dimensions must match", stderr)

            code, _, stderr = self.run_main([
                "--prompt", "edit", "--mode", "edit", "--input", str(input_path), "--mask", str(wrong_format), "--dry-run"
            ])
            self.assertEqual(code, 1)
            self.assertIn("must be image/png", stderr)

    def test_mask_must_be_smaller_than_four_megabytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.png"
            mask_path = root / "mask.png"
            write_png(input_path)
            write_png(mask_path)
            with patch.object(image_api, "MAX_MASK_BYTES", mask_path.stat().st_size):
                with self.assertRaisesRegex(ValueError, "smaller than 4 MB"):
                    image_api.validate_edit_inputs([str(input_path)], str(mask_path))

    def test_png_mask_is_accepted_with_same_size_jpeg_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.jpg"
            mask_path = root / "mask.png"
            Image.new("RGB", (4, 4), "red").save(input_path, format="JPEG")
            write_png(mask_path, (4, 4), "RGBA")
            inputs, mask = image_api.validate_edit_inputs([str(input_path)], str(mask_path))
        self.assertEqual(inputs[0]["mime_type"], "image/jpeg")
        self.assertEqual(mask["mime_type"], "image/png")

    def test_malformed_models_response_is_not_reported_as_success(self) -> None:
        malformed = json.dumps({"object": "list", "data": [{"id": "gpt-image-2"}]}).encode()
        with patch.object(image_api, "open_without_redirect") as open_response:
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = malformed
            open_response.return_value = response
            code = image_api.connection_check("https://provider.example/v1", "test-key")
        self.assertEqual(code, 1)

    def test_provider_image_redirect_does_not_receive_authorization(self) -> None:
        with mock_server(png_bytes(), content_type="image/png", image_redirects=True) as server:
            url = f"http://127.0.0.1:{server.server_port}/image"
            self.assertEqual(image_api.image_item_response_type({"url": url}), "url")
            data, source_url = image_api.decode_image_item({"url": url}, 0)
            self.assertEqual(data, png_bytes())
            self.assertEqual(source_url, url)
            self.assertFalse(any(item.get("authorization") for item in MockHandler.requests))

    def test_provider_image_url_redirect_is_followed_without_auth(self) -> None:
        with mock_server(png_bytes(), content_type="image/png", image_redirects=True) as server:
            data = image_api.download_provider_image(f"http://127.0.0.1:{server.server_port}/image")
            self.assertEqual(data, png_bytes())
            self.assertFalse(any(item.get("authorization") for item in MockHandler.requests))

    def test_authenticated_api_redirect_is_not_followed(self) -> None:
        with mock_server(b"redirect", redirect_posts=True) as server, tempfile.TemporaryDirectory() as temporary:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            with self.assertRaises(ValueError):
                image_api.request_image(
                    base,
                    "test-key-that-must-not-leak",
                    "generations",
                    {"model": "gpt-image-2", "prompt": "test", "n": 1},
                    [],
                    None,
                    Path(temporary),
                    10,
                )
            self.assertFalse(MockHandler.redirect_target_hit)
            self.assertEqual(MockHandler.requests[0]["authorization"], "Bearer test-key-that-must-not-leak")

    def test_generation_saves_images_and_manifest(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        with tempfile.TemporaryDirectory() as temporary, mock_server(body) as server:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            output_dir = Path(temporary) / "run"
            with patch.dict(os.environ, {"IMAGE_API_BASE_URL": base}, clear=False):
                code, stdout, stderr = self.run_main([
                    "--prompt", "test", "--model", "gpt-image-2", "--output-dir", str(output_dir), "--timeout", "10"
                ])
            self.assertEqual(code, 0, stderr)
            manifest = json.loads((output_dir / "image-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["outputs"]), 1)
            self.assertEqual(manifest["outputs"][0]["response_type"], "b64_json")
            self.assertTrue((output_dir / "image-1.png").is_file())
            self.assertEqual(inspect_output.inspect_directory(output_dir)[1], True)
            request = MockHandler.requests[0]
            self.assertEqual(request["authorization"], "Bearer test-key-that-must-not-leak")
            self.assertNotIn("test-key-that-must-not-leak", stdout)
            self.assertNotIn("test-key-that-must-not-leak", (output_dir / "image-manifest.json").read_text(encoding="utf-8"))

    def test_response_count_must_match_requested_count(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        with mock_server(body) as server, tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "request asked for 2"):
                image_api.request_image(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "test-key",
                    "generations",
                    {"model": "gpt-image-2", "prompt": "test", "n": 2},
                    [],
                    None,
                    Path(temporary),
                    10,
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_transparent_output_requires_decoded_alpha(self) -> None:
        encoded = base64.b64encode(png_bytes(mode="RGB")).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        with mock_server(body) as server, tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "alpha channel"):
                image_api.request_image(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "test-key",
                    "generations",
                    {
                        "model": "gpt-image-2",
                        "prompt": "test",
                        "n": 1,
                        "background": "transparent",
                        "output_format": "png",
                    },
                    [],
                    None,
                    Path(temporary),
                    10,
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_invalid_later_response_removes_earlier_saved_images(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}, {"b64_json": "bm90LWltYWdl"}]}).encode()
        with mock_server(body) as server, tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "not a recognized raster image"):
                image_api.request_image(
                    f"http://127.0.0.1:{server.server_port}/v1",
                    "test-key",
                    "generations",
                    {"model": "gpt-image-2", "prompt": "test", "n": 2},
                    [],
                    None,
                    Path(temporary),
                    10,
                )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_manifest_write_failure_removes_generated_image(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        original_replace = Path.replace

        def fail_manifest_replace(path: Path, target: Path) -> Path:
            if path.name.startswith(".image-manifest."):
                raise OSError("simulated manifest write failure")
            return original_replace(path, target)

        with mock_server(body) as server, tempfile.TemporaryDirectory() as temporary:
            with patch.object(Path, "replace", fail_manifest_replace):
                with self.assertRaisesRegex(OSError, "simulated manifest write failure"):
                    image_api.request_image(
                        f"http://127.0.0.1:{server.server_port}/v1",
                        "test-key",
                        "generations",
                        {"model": "gpt-image-2", "prompt": "test", "n": 1},
                        [],
                        None,
                        Path(temporary),
                        10,
                    )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_existing_output_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "run"
            output_dir.mkdir()
            (output_dir / "old.png").write_bytes(png_bytes())
            code, _, stderr = self.run_main([
                "--prompt", "test", "--model", "gpt-image-2", "--output-dir", str(output_dir)
            ])
        self.assertEqual(code, 1)
        self.assertIn("must be empty", stderr)

    def test_inspector_reports_unlisted_image(self) -> None:
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        with tempfile.TemporaryDirectory() as temporary, mock_server(body) as server:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            output_dir = Path(temporary) / "run"
            with patch.dict(os.environ, {"IMAGE_API_BASE_URL": base}, clear=False):
                code, _, stderr = self.run_main([
                    "--prompt", "test", "--model", "gpt-image-2", "--output-dir", str(output_dir)
                ])
            self.assertEqual(code, 0, stderr)
            (output_dir / "stale.png").write_bytes(png_bytes())
            result, ok = inspect_output.inspect_directory(output_dir)
        self.assertFalse(ok)
        self.assertFalse(result["ok"])
        self.assertIn("stale.png", " ".join(result["errors"]))


if __name__ == "__main__":
    unittest.main()
