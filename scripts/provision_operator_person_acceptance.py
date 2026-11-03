"""Download hash-admitted Apache model/fixture into owned development tools only."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

from k5vision.operator_analytics_person import _manifest

_FIXTURE_URL = (
    "https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/"
    "6697dead54ed1cdd664b0313189c2cb52ee6335e/models/intel/"
    "person-detection-retail-0013/assets/person-detection-retail-0013.png"
)
_FIXTURE_SHA = "2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959"


def download(url: str, path: Path, size: int, algorithm: str, expected: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url, timeout=120) as response:
        payload = response.read(size + 1)
    if len(payload) != size or hashlib.new(algorithm, payload).hexdigest() != expected:
        raise ValueError("Person development artifact identity was refused")
    with path.open("xb") as output:
        output.write(payload)


def main() -> None:
    root = Path(os.environ["K5_DONOR_TOOL_ROOT"]) / "person development only"
    root.mkdir()
    model_root = root / "models"
    for artifact in _manifest()["artifacts"]:
        download(
            artifact["source_url"],
            model_root / artifact["relative_path"],
            artifact["bytes"],
            "sha384",
            artifact["sha384"],
        )
    fixture = root / "official-person-fixture.png"
    download(_FIXTURE_URL, fixture, 553577, "sha256", _FIXTURE_SHA)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write(f"K5_OPERATOR_PERSON_ARTIFACT_ROOT={model_root}\n")
        output.write(f"K5_OPERATOR_PERSON_FIXTURE={fixture}\n")
    print("Pinned Apache person model and official fixture admitted for development acceptance")


if __name__ == "__main__":
    main()
