import pytest
from pydantic import ValidationError

from k5vision.adapters.stage03_process import ProcessCandidateSpec


def test_process_spec_rejects_source_as_executable() -> None:
    with pytest.raises(ValidationError, match="process executable must be fixed"):
        ProcessCandidateSpec(candidate="candidate-a", argv=["{source}"])
