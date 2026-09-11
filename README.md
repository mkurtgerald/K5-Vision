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

## License

K5 Vision is currently distributed under the MIT License. See `LICENSE`.
