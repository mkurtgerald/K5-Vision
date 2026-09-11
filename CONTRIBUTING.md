# Contributing to K5 Vision

Thank you for your interest in contributing! We follow a lean, quality-first approach.

## Code of Conduct

- Be respectful and professional
- Focus on code quality over speed
- Test before submitting PRs
- Document your changes

## Process

### 1. Fork & Branch

```bash
git clone https://github.com/YOUR_USERNAME/K5-Vision.git
cd K5-Vision
git checkout -b feature/your-feature-name
```

### 2. Development

- All new features require tests
- Tests must pass locally before PR submission
- Code must follow PEP 8 style guidelines

```bash
# Run tests
pytest tests/ -v --cov=src

# Format code
black src/ tests/

# Lint
flake8 src/ tests/
```

### 3. Commit & Push

```bash
git add .
git commit -m "feat: describe your change"
git push origin feature/your-feature-name
```

### 4. Pull Request

- Fill out PR template completely
- Link related issues
- All CI/CD checks must pass
- At least one maintainer approval required

### Quality Standards

- **Test Coverage**: Minimum 80% for new code
- **Documentation**: All public APIs must be documented
- **Type Hints**: Use Python type annotations
- **No Breaking Changes**: Unless explicitly versioned

## Sprint Cycles

- Weekly cycles (Monday plan → Friday ship)
- Each phase is independently tested and tagged
- No moving forward with failing tests

## Questions?

Open an issue or start a discussion in the repository.

## License

By contributing, you agree your code will be licensed under MIT.
