"""Office-to-PDF conversion using LibreOffice in a temporary user profile."""

from __future__ import annotations

import subprocess
from pathlib import Path

SUPPORTED_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx"}


def convert_office_to_pdf(source: str | Path, output_dir: str | Path, timeout: int = 120) -> Path:
    """Convert a Word or Excel document to PDF and return the generated path."""
    source_path = Path(source).resolve()
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if source_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("unsupported document extension")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    profile_dir = destination / "lo-profile"
    profile_dir.mkdir(exist_ok=True)
    profile_uri = profile_dir.as_uri()
    command = [
        "libreoffice", "--headless", "--nologo", "--nodefault", "--nolockcheck",
        "-env:UserInstallation=" + profile_uri,
        "--convert-to", "pdf", "--outdir", str(destination), str(source_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    pdf_path = destination / (source_path.stem + ".pdf")
    if result.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "LibreOffice conversion failed").strip()
        raise RuntimeError(detail[:1000])
    return pdf_path
