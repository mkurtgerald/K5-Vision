import json
from pathlib import Path

from k5vision.adapters.stage03_process import ProcessCandidateSpec

_CONFIG = Path("config/stage03-candidates.windows.json")


def test_versioned_stage03_candidates_are_comparative_and_source_free() -> None:
    raw = json.loads(_CONFIG.read_text(encoding="utf-8"))
    specs = [ProcessCandidateSpec.model_validate(item) for item in raw]

    assert len(specs) >= 2
    assert len({spec.candidate for spec in specs}) == len(specs)

    serialized = json.dumps(raw).casefold()
    assert "rtsp://" not in serialized
    assert "username" not in serialized
    assert "password" not in serialized

    for spec in specs:
        assert spec.argv.count("{source}") == 1
        assert spec.argv[0] != "{source}"
        if len(spec.argv) >= 2 and spec.argv[1].startswith("tools/"):
            assert Path(spec.argv[1]).is_file()
