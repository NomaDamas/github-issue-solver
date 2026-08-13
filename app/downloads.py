from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, TypedDict

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse


class DownloadApp(TypedDict):
    name: str
    platform: str
    version: str
    updated: str
    size: str
    sha256: str
    url: str
    repo: str
    description: str


@dataclass(frozen=True, slots=True)
class DownloadFile:
    path: Path
    media_type: str
    filename: str
    missing_detail: str


APK_DIST_DIR: Final = Path("/home/cheol/projects/mobile-app-agent/.apk-dist")
OPENMINIS_APK: Final = APK_DIST_DIR / "OpenMinis-latest.apk"
OPENMINIS_MANIFEST: Final = APK_DIST_DIR / "OpenMinis-latest.json"
DOLSHOI_APK: Final = APK_DIST_DIR / "Dolshoi-latest.apk"
DOLSHOI_MANIFEST: Final = APK_DIST_DIR / "Dolshoi-latest.json"
RECOVERY_APK: Final = APK_DIST_DIR / "Dolshoi-2.19.19-recovery.apk"
LANGGATE_APK: Final = APK_DIST_DIR / "LangGate-latest.apk"
LANGGATE_MANIFEST: Final = APK_DIST_DIR / "LangGate-latest.json"
APK_MEDIA_TYPE: Final = "application/vnd.android.package-archive"

OPENMINIS_APK_FILE: Final = DownloadFile(
    path=OPENMINIS_APK,
    media_type=APK_MEDIA_TYPE,
    filename="OpenMinis-latest.apk",
    missing_detail="OpenMinis APK is not available",
)
OPENMINIS_MANIFEST_FILE: Final = DownloadFile(
    path=OPENMINIS_MANIFEST,
    media_type="application/json",
    filename="OpenMinis-latest.json",
    missing_detail="OpenMinis update manifest is not available",
)

DOLSHOI_APK_FILE: Final = DownloadFile(
    path=DOLSHOI_APK,
    media_type=APK_MEDIA_TYPE,
    filename="Dolshoi-latest.apk",
    missing_detail="Dolshoi APK is not available",
)
RECOVERY_APK_FILE: Final = DownloadFile(
    path=RECOVERY_APK,
    media_type=APK_MEDIA_TYPE,
    filename="Dolshoi-2.19.19-recovery.apk",
    missing_detail="Dolshoi recovery APK is not available",
)
DOLSHOI_MANIFEST_FILE: Final = DownloadFile(
    path=DOLSHOI_MANIFEST,
    media_type="application/json",
    filename="Dolshoi-latest.json",
    missing_detail="Dolshoi update manifest is not available",
)
LANGGATE_APK_FILE: Final = DownloadFile(
    path=LANGGATE_APK,
    media_type=APK_MEDIA_TYPE,
    filename="LangGate-latest.apk",
    missing_detail="LangGate APK is not available",
)
LANGGATE_MANIFEST_FILE: Final = DownloadFile(
    path=LANGGATE_MANIFEST,
    media_type="application/json",
    filename="LangGate-latest.json",
    missing_detail="LangGate update manifest is not available",
)


def _human_size(path: Path) -> str:
    if not path.is_file():
        return "pending"
    return f"{path.stat().st_size / 1024 / 1024:.1f} MB"


def _updated_day(path: Path) -> str:
    if not path.is_file():
        return "pending"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _manifest_value(path: Path, key: str, fallback: str) -> str:
    if not path.is_file():
        return fallback
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return fallback
    if not isinstance(raw, dict):
        return fallback
    value = raw.get(key)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return fallback


def get_download_apps() -> list[DownloadApp]:
    return [
        {
        "name": "OpenMinis",
        "platform": "Android APK",
        "version": _manifest_value(OPENMINIS_MANIFEST, "versionName", "0.20-preview"),
        "updated": _updated_day(OPENMINIS_APK),
        "size": _human_size(OPENMINIS_APK),
        "sha256": _manifest_value(OPENMINIS_MANIFEST, "sha256", "pending"),
        "url": "/downloads/openminis/latest.apk",
        "repo": "local:/home/cheol/projects/openminis",
        "description": "OpenMinis Android 모바일-use 통합 빌드. 검증된 최신 디버그 APK를 제공합니다.",
        },
        {
        "name": "Dolshoi",
        "platform": "Android APK",
        "version": _manifest_value(DOLSHOI_MANIFEST, "versionName", "2.19.9"),
        "updated": _updated_day(DOLSHOI_APK),
        "size": _human_size(DOLSHOI_APK),
        "sha256": _manifest_value(DOLSHOI_MANIFEST, "sha256", "pending"),
        "url": "/downloads/dolshoi/latest.apk",
        "repo": "local:/home/cheol/projects/dolshoi-android",
        "description": "Dolshoi Android 모바일 에이전트. 앱 설정의 업데이트 버튼은 이 최신 APK manifest를 사용합니다.",
        },
        {
        "name": "LangGate",
        "platform": "Android APK",
        "version": _manifest_value(LANGGATE_MANIFEST, "versionName", "1.1.6"),
        "updated": _updated_day(LANGGATE_APK),
        "size": _human_size(LANGGATE_APK),
        "sha256": _manifest_value(LANGGATE_MANIFEST, "sha256", "0eb9ee4baa9fc3047b6ae315269ccfbfafdf3c5063468226913ddcd8da3dee7e"),
        "url": "/downloads/langgate/latest.apk",
        "repo": "local:/home/cheol/projects/english-disney+",
        "description": "OTT 언어 게이트 앱. 설치 후 앱 홈의 업데이트 확인 버튼으로 최신 APK를 확인합니다.",
        },
    ]


DOWNLOAD_APPS: Final[list[DownloadApp]] = get_download_apps()


def _file_response(download_file: DownloadFile) -> FileResponse:
    if not download_file.path.is_file():
        raise HTTPException(404, download_file.missing_detail)
    return FileResponse(
        download_file.path,
        media_type=download_file.media_type,
        filename=download_file.filename,
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


def _head_response(download_file: DownloadFile) -> Response:
    if not download_file.path.is_file():
        raise HTTPException(404, download_file.missing_detail)
    return Response(
        status_code=200,
        media_type=download_file.media_type,
        headers={
            "Cache-Control": "no-store, must-revalidate",
            "Content-Length": str(download_file.path.stat().st_size),
        },
    )


def download_openminis_apk() -> FileResponse:
    return _file_response(OPENMINIS_APK_FILE)


def download_openminis_apk_head() -> Response:
    return _head_response(OPENMINIS_APK_FILE)


def download_openminis_manifest() -> FileResponse:
    return _file_response(OPENMINIS_MANIFEST_FILE)


def download_openminis_manifest_head() -> Response:
    return _head_response(OPENMINIS_MANIFEST_FILE)


def download_dolshoi_apk() -> FileResponse:
    return _file_response(DOLSHOI_APK_FILE)


def download_dolshoi_apk_head() -> Response:
    return _head_response(DOLSHOI_APK_FILE)


def download_dolshoi_recovery_apk() -> FileResponse:
    return _file_response(RECOVERY_APK_FILE)


def download_dolshoi_recovery_apk_head() -> Response:
    return _head_response(RECOVERY_APK_FILE)


def download_dolshoi_manifest() -> FileResponse:
    return _file_response(DOLSHOI_MANIFEST_FILE)


def download_dolshoi_manifest_head() -> Response:
    return _head_response(DOLSHOI_MANIFEST_FILE)


def download_langgate_apk() -> FileResponse:
    return _file_response(LANGGATE_APK_FILE)


def download_langgate_apk_head() -> Response:
    return _head_response(LANGGATE_APK_FILE)


def download_langgate_manifest() -> FileResponse:
    return _file_response(LANGGATE_MANIFEST_FILE)


def download_langgate_manifest_head() -> Response:
    return _head_response(LANGGATE_MANIFEST_FILE)


def register_download_routes(app: FastAPI) -> None:
    app.add_api_route("/downloads/openminis/latest.apk", download_openminis_apk, methods=["GET"])
    app.add_api_route("/openminis/latest.apk", download_openminis_apk, methods=["GET"])
    app.add_api_route("/downloads/openminis/latest.apk", download_openminis_apk_head, methods=["HEAD"])
    app.add_api_route("/openminis/latest.apk", download_openminis_apk_head, methods=["HEAD"])
    app.add_api_route("/downloads/openminis/latest.json", download_openminis_manifest, methods=["GET"])
    app.add_api_route("/openminis/latest.json", download_openminis_manifest, methods=["GET"])
    app.add_api_route("/downloads/openminis/latest.json", download_openminis_manifest_head, methods=["HEAD"])
    app.add_api_route("/openminis/latest.json", download_openminis_manifest_head, methods=["HEAD"])
    app.add_api_route("/downloads/dolshoi/latest.apk", download_dolshoi_apk, methods=["GET"])
    app.add_api_route("/dolshoi/latest.apk", download_dolshoi_apk, methods=["GET"])
    app.add_api_route("/downloads/dolshoi/latest.apk", download_dolshoi_apk_head, methods=["HEAD"])
    app.add_api_route("/dolshoi/latest.apk", download_dolshoi_apk_head, methods=["HEAD"])
    app.add_api_route("/dolshoi/recovery.apk", download_dolshoi_recovery_apk, methods=["GET"])
    app.add_api_route("/dolshoi/recovery.apk", download_dolshoi_recovery_apk_head, methods=["HEAD"])
    app.add_api_route("/downloads/dolshoi/latest.json", download_dolshoi_manifest, methods=["GET"])
    app.add_api_route("/dolshoi/latest.json", download_dolshoi_manifest, methods=["GET"])
    app.add_api_route("/downloads/dolshoi/latest.json", download_dolshoi_manifest_head, methods=["HEAD"])
    app.add_api_route("/dolshoi/latest.json", download_dolshoi_manifest_head, methods=["HEAD"])
    app.add_api_route("/downloads/langgate/latest.apk", download_langgate_apk, methods=["GET"])
    app.add_api_route("/langgate/latest.apk", download_langgate_apk, methods=["GET"])
    app.add_api_route("/downloads/langgate/latest.apk", download_langgate_apk_head, methods=["HEAD"])
    app.add_api_route("/langgate/latest.apk", download_langgate_apk_head, methods=["HEAD"])
    app.add_api_route("/downloads/langgate/latest.json", download_langgate_manifest, methods=["GET"])
    app.add_api_route("/langgate/latest.json", download_langgate_manifest, methods=["GET"])
    app.add_api_route("/downloads/langgate/latest.json", download_langgate_manifest_head, methods=["HEAD"])
    app.add_api_route("/langgate/latest.json", download_langgate_manifest_head, methods=["HEAD"])
