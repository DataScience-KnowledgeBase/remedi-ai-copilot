"""Verify the private ADK artifact and its pinned upstream-audit identity."""

import hashlib
import json
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

from packaging.requirements import Requirement


def main():
    root = Path(__file__).resolve().parents[1]
    vendor = root / "backend" / "vendor"
    provenance = json.loads((vendor / "provenance.json").read_text(encoding="utf-8"))
    wheel = vendor / provenance["wheel"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest != provenance["sha256"]:
        raise RuntimeError("Private ADK wheel hash does not match source provenance.")
    if digest not in (root / "backend" / "requirements.lock").read_text(encoding="utf-8"):
        raise RuntimeError("Private ADK artifact hash is absent from the runtime lock.")
    with ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith("/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode())
        if metadata["Name"] != "google-adk" or metadata["Version"] != provenance["version"]:
            raise RuntimeError("Private ADK wheel metadata does not match provenance.")
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in archive.namelist()):
            raise RuntimeError("Private ADK artifact must retain its upstream license.")
        requirements = [
            Requirement(value) for value in metadata.get_all("Requires-Dist", [])
        ]
        for name in ("opentelemetry-api", "opentelemetry-sdk"):
            requirement = next(item for item in requirements if item.name == name)
            if "1.45.1" not in requirement.specifier or "1.45.0" in requirement.specifier:
                raise RuntimeError("Private ADK must require the patched OpenTelemetry release.")
    audit_lines = (vendor / "upstream-audit.txt").read_text(encoding="utf-8").splitlines()
    audit_requirements = [line for line in audit_lines if line and not line.startswith("#")]
    if audit_requirements != [f"google-adk=={provenance['upstream_version']}"]:
        raise RuntimeError("Upstream audit input does not match the private artifact base.")
    print(f"Verified private ADK {provenance['version']}; SHA256 {digest}")


if __name__ == "__main__":
    main()
