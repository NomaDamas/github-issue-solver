from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import agents
from app import downloads
from app import main
from app import orchestrator

from app import package_lake_service

class RegressionTests(unittest.TestCase):
    def test_gjc_agent_runs_ephemeral_non_interactive(self) -> None:
        captured = {}

        def fake_run(cmd, cwd, env, input, text, stdout, stderr, timeout):  # noqa: A002
            captured.update(cmd=cmd, cwd=cwd, env=env, input=input, timeout=timeout)
            prompt_arg = cmd[-1]
            self.assertTrue(prompt_arg.startswith("@"))
            self.assertEqual(Path(prompt_arg[1:]).read_text(encoding="utf-8"), "do the work")
            return SimpleNamespace(returncode=0, stdout="ok")

        old_gh_token = os.environ.get("GH_TOKEN")
        old_github_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GH_TOKEN"] = "stale"
        os.environ["GITHUB_TOKEN"] = "stale"
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                patch.object(agents, "resolve_command", lambda binary: f"/bin/{binary}"), \
                patch.object(agents.subprocess, "run", fake_run):
                result = agents.run_agent("gjc", Path(tmp), "do the work", 123)
        finally:
            if old_gh_token is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = old_gh_token
            if old_github_token is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old_github_token

        self.assertTrue(result.ok)
        self.assertEqual(captured["cmd"][:6], ["/bin/gjc", "-p", "--mode", "text", "--no-session", "--no-pty"])
        self.assertIsNone(captured["input"])
        self.assertEqual(captured["env"]["GJC_NO_PTY"], "1")
        self.assertNotIn("GH_TOKEN", captured["env"])
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertFalse(Path(captured["cmd"][-1][1:]).exists())

    def test_reset_workspace_discards_dirty_tree_before_checkout(self) -> None:
        calls = []

        def fake_run_cmd(args, cwd, token="", timeout=300):
            calls.append((args, cwd, token, timeout))
            return ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(orchestrator, "run_cmd", fake_run_cmd):
            tmp_path = Path(tmp)
            orchestrator.reset_workspace_for_checkout(tmp_path, "feature", "tok")

        self.assertEqual(
            calls,
            [
                (["git", "reset", "--hard"], tmp_path, "tok", 300),
                (["git", "clean", "-fd"], tmp_path, "tok", 300),
                (["git", "checkout", "feature"], tmp_path, "tok", 300),
            ],
        )

    def test_infra_verify_failure_does_not_mark_verification_failed(self) -> None:
        updates = []

        class FakeConn:
            def execute(self, sql, params=()):
                updates.append((" ".join(sql.split()), params))
                if sql.startswith("SELECT * FROM jobs"):
                    return SimpleNamespace(fetchone=lambda: {"id": 7, "type": "verify", "issue_id": 42})
                return SimpleNamespace(fetchone=lambda: None)

        class FakeDb:
            def __enter__(self):
                return FakeConn()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(orchestrator, "db", lambda: FakeDb()), patch.object(orchestrator, "append_job_log"):
            orchestrator.fail_job(7, RuntimeError("checkout failed"))

        issue_updates = [params for sql, params in updates if sql.startswith("UPDATE issues SET status=")]
        self.assertEqual(issue_updates, [("verifying", 42)])
        self.assertFalse(any(params and params[0] == "verification_failed" for _sql, params in updates))

    def test_markr_portal_exposes_tailnet_demo_services(self) -> None:
        response = main.index()
        html = response.body.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("데모 서비스", html)
        self.assertIn("Company Agent 통합 콘솔", html)
        self.assertIn("https://cheol-nucbox-evo-x2.taildf528d.ts.net", html)
        self.assertIn("Dify 앱 스튜디오", html)
        self.assertIn("https://cheol-nucbox-evo-x2.taildf528d.ts.net:8444/", html)
        self.assertIn("vLLM API 문서", html)
        self.assertIn("https://cheol-nucbox-evo-x2.taildf528d.ts.net:8445/docs", html)
        self.assertIn('target="_blank" rel="noopener"', html)

    def test_markr_portal_exposes_package_lake_sso_card(self) -> None:
        response = main.index()
        html = response.body.decode("utf-8")

        self.assertIn("Package Lake", html)
        self.assertIn("PL", html)
        self.assertIn(package_lake_service.PACKAGE_LAKE_ENDPOINT, html)
        self.assertIn("/api/services/package-lake/launch", html)
        self.assertIn("MarkerAI 관리 콘솔 SSO · USER 최소 권한", html)

    def test_package_lake_launch_url_uses_short_lived_user_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "launch-secret"
            secret_file.write_text("s" * 32, encoding="utf-8")
            with patch.object(package_lake_service, "LAUNCH_SECRET_FILE", str(secret_file)):
                launch_url = package_lake_service.create_launch_url("cheol", now=1_700_000_000)

        self.assertTrue(launch_url.startswith(f"{package_lake_service.PACKAGE_LAKE_ENDPOINT}/sso/launch?"))
        token = launch_url.split("launch_token=", 1)[1].split("&", 1)[0]
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = __import__("json").loads(__import__("base64").urlsafe_b64decode(payload))
        self.assertEqual(claims["sub"], "markr-console:cheol")
        self.assertEqual(claims["tenant_id"], "markerai-management")
        self.assertEqual(claims["roles"], ["USER"])
        self.assertEqual(claims["exp"] - claims["iat"], 60)

    def test_markr_download_center_exposes_langgate(self) -> None:
        response = main.index()
        html = response.body.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("LangGate", html)
        self.assertIn("/downloads/langgate/latest.apk", html)

    def test_markr_download_center_exposes_openminis(self) -> None:
        response = main.index()
        html = response.body.decode("utf-8")
        openminis = next(app for app in downloads.get_download_apps() if app["name"] == "OpenMinis")

        self.assertEqual(response.status_code, 200)
        self.assertIn("OpenMinis", html)
        self.assertIn(openminis["version"], html)
        self.assertIn("/downloads/openminis/latest.apk", html)
        self.assertIn("local:/home/cheol/projects/openminis", html)

    def test_markr_download_center_exposes_dolshoi_instead_of_hahnee(self) -> None:
        response = main.index()
        html = response.body.decode("utf-8")
        dolshoi = next(app for app in downloads.get_download_apps() if app["name"] == "Dolshoi")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dolshoi", html)
        self.assertIn(dolshoi["version"], html)
        self.assertIn("/downloads/dolshoi/latest.apk", html)
        self.assertNotIn('"name": "Hahnee"', html)
        self.assertNotIn('"version": "2.19.2"', html)

    def test_langgate_download_routes_support_app_and_browser_clients(self) -> None:
        apk_body = b"apk-bytes"
        manifest_body = b'{"versionName":"1.1.0"}'

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            apk = tmp_path / "LangGate-latest.apk"
            manifest = tmp_path / "LangGate-latest.json"
            apk.write_bytes(apk_body)
            manifest.write_bytes(manifest_body)

            apk_file = downloads.DownloadFile(
                path=apk,
                media_type=downloads.APK_MEDIA_TYPE,
                filename="LangGate-latest.apk",
                missing_detail="LangGate APK is not available",
            )
            manifest_file = downloads.DownloadFile(
                path=manifest,
                media_type="application/json",
                filename="LangGate-latest.json",
                missing_detail="LangGate update manifest is not available",
            )

            with patch.object(downloads, "LANGGATE_APK_FILE", apk_file), patch.object(downloads, "LANGGATE_MANIFEST_FILE", manifest_file):
                for base_path in ("/downloads/langgate", "/langgate"):
                    apk_methods = set()
                    manifest_methods = set()
                    for route in main.app.routes:
                        if getattr(route, "path", "") == f"{base_path}/latest.apk":
                            apk_methods.update(getattr(route, "methods", set()))
                        if getattr(route, "path", "") == f"{base_path}/latest.json":
                            manifest_methods.update(getattr(route, "methods", set()))

                    self.assertIn("GET", apk_methods)
                    self.assertIn("HEAD", apk_methods)
                    self.assertIn("GET", manifest_methods)
                    self.assertIn("HEAD", manifest_methods)

                    apk_head = downloads.download_langgate_apk_head()
                    self.assertEqual(apk_head.status_code, 200)
                    self.assertEqual(apk_head.headers["content-length"], str(len(apk_body)))
                    self.assertEqual(apk_head.headers["content-type"], downloads.APK_MEDIA_TYPE)

                    apk_get = downloads.download_langgate_apk()
                    self.assertEqual(apk_get.status_code, 200)
                    self.assertEqual(Path(apk_get.path), apk)

                    manifest_head = downloads.download_langgate_manifest_head()
                    self.assertEqual(manifest_head.status_code, 200)
                    self.assertEqual(manifest_head.headers["content-length"], str(len(manifest_body)))
                    self.assertEqual(manifest_head.headers["content-type"], "application/json")

                    manifest_get = downloads.download_langgate_manifest()
                    self.assertEqual(manifest_get.status_code, 200)
                    self.assertEqual(Path(manifest_get.path), manifest)

    def test_dolshoi_download_routes_support_app_and_browser_clients(self) -> None:
        apk_body = b"dolshoi-apk-bytes"
        manifest_body = b'{"versionName":"2.19.9","versionCode":65}'

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            apk = tmp_path / "Dolshoi-latest.apk"
            manifest = tmp_path / "Dolshoi-latest.json"
            apk.write_bytes(apk_body)
            manifest.write_bytes(manifest_body)

            apk_file = downloads.DownloadFile(
                path=apk,
                media_type=downloads.APK_MEDIA_TYPE,
                filename="Dolshoi-latest.apk",
                missing_detail="Dolshoi APK is not available",
            )
            manifest_file = downloads.DownloadFile(
                path=manifest,
                media_type="application/json",
                filename="Dolshoi-latest.json",
                missing_detail="Dolshoi update manifest is not available",
            )

            with patch.object(downloads, "DOLSHOI_APK_FILE", apk_file), patch.object(downloads, "DOLSHOI_MANIFEST_FILE", manifest_file):
                for base_path in ("/downloads/dolshoi", "/dolshoi"):
                    apk_methods = set()
                    manifest_methods = set()
                    for route in main.app.routes:
                        if getattr(route, "path", "") == f"{base_path}/latest.apk":
                            apk_methods.update(getattr(route, "methods", set()))
                        if getattr(route, "path", "") == f"{base_path}/latest.json":
                            manifest_methods.update(getattr(route, "methods", set()))

                    self.assertIn("GET", apk_methods)
                    self.assertIn("HEAD", apk_methods)
                    self.assertIn("GET", manifest_methods)
                    self.assertIn("HEAD", manifest_methods)

                    apk_head = downloads.download_dolshoi_apk_head()
                    self.assertEqual(apk_head.status_code, 200)
                    self.assertEqual(apk_head.headers["content-length"], str(len(apk_body)))
                    self.assertEqual(apk_head.headers["content-type"], downloads.APK_MEDIA_TYPE)

                    apk_get = downloads.download_dolshoi_apk()
                    self.assertEqual(apk_get.status_code, 200)
                    self.assertEqual(Path(apk_get.path), apk)

                    manifest_head = downloads.download_dolshoi_manifest_head()
                    self.assertEqual(manifest_head.status_code, 200)
                    self.assertEqual(manifest_head.headers["content-length"], str(len(manifest_body)))
                    self.assertEqual(manifest_head.headers["content-type"], "application/json")

                    manifest_get = downloads.download_dolshoi_manifest()
                    self.assertEqual(manifest_get.status_code, 200)
                    self.assertEqual(Path(manifest_get.path), manifest)


if __name__ == "__main__":
    unittest.main()
