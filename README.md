# K5 Vision

K5 Vision is an experimental modular integration platform focused on stable interfaces, deterministic behavior, and reproducible validation.

Public repository material is intentionally limited to information required to build, test, and review the current stage. Forward-looking product scope and internal roadmap details are not maintained here.

## Development

Requirements: Python 3.12+

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn k5vision.main:app --reload
```

Then open `http://127.0.0.1:8000/docs`.

## Delivery model

Work proceeds through strict precursor gates. A downstream stage does not begin until its required predecessor is complete, accepted, and green. See `docs/DELIVERY_GATES.md`.

## Licensing and donor code

K5 Vision is publicly viewable but is not, as a whole, an open-source project.

Original K5-authored material released under the current repository license is governed by the K5 Vision Source-Available License in `LICENSE`.

Third-party and donor material remains governed by its original license. K5 Vision may use permissively licensed donor components such as MIT, BSD, ISC, and Apache-2.0 material when reviewed under `docs/COMMERCIAL_DEPENDENCY_POLICY.md`. Required donor notices and provenance must be preserved.

Historical K5 material that was previously published under the MIT License retains the rights already granted for that historical version.

See `docs/DONOR_LEDGER.md` and `THIRD_PARTY_NOTICES.md` for provenance and attribution records.
