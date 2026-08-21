# Contributing to Smart Attendance System

Thank you for your interest in contributing! This document provides guidelines and information for contributors.

## 🚀 Getting Started

1. **Fork the repository**
2. **Clone your fork:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/smart-attendance-system.git
   cd smart-attendance-system
   ```
3. **Set up the development environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -e ".[dev]"    # installs ruff, mypy, pytest, pre-commit, etc.
   pre-commit install         # runs ruff (lint + format) on every commit
   ```
4. **Create a branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```

## 📝 Commit Convention

We use [Conventional Commits](https://www.conventionalcommits.org/). Please format your commit messages as:

```
<type>(<scope>): <description>

[optional body]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples:**
```
feat(auth): add login functionality
fix(camera): resolve webcam initialization error
docs(readme): update installation instructions
```

## 🔀 Pull Request Process

1. **Update documentation** if needed
2. **Add tests** for new functionality
3. **Run the full gate locally** (this is what CI enforces, so a green run here means a green CI):
   ```bash
   ruff check . && ruff format --check . && mypy app/ && pytest -q --cov=app
   ```
4. **Follow the PR template** when creating your PR
5. **Request review** from maintainers

## 🧪 Testing

Tests live under `tests/unit/` and `tests/integration/`, on an in-memory SQLite
`TestingConfig` (fixtures in `tests/conftest.py`). Run them before submitting:

```bash
# Whole suite, quiet, with coverage (75% floor, enforced by CI and pyproject.toml)
pytest -q --cov=app

# A single area
pytest tests/integration/test_auth.py -v

# Hardware-dependent tests (a real camera) are marked and deselected by default:
pytest -m hardware        # opt in locally
```

The suite also runs against PostgreSQL in CI; see
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#postgresql) to reproduce that leg.

## 📋 Code Style

Ruff is the single source of truth for lint **and** formatting; mypy type-checks `app/`.
All config lives in `pyproject.toml` — do not add `flake8`, `black`, or `isort`, which the
project deliberately replaced (one tool, one config, so local and CI agree).

```bash
ruff check .           # lint (includes import sorting and security rules)
ruff check . --fix     # autofix what it can
ruff format .          # format
mypy app/              # types

# Security scanners CI also gates on:
bandit -c pyproject.toml -r app/ wsgi.py run.py gunicorn.conf.py
pip-audit --strict --requirement requirements.txt
```

`pre-commit install` wires ruff into your commits with the same pinned versions as CI. New
dependencies must be pinned in both `pyproject.toml` and `requirements.txt`.

## 🐛 Reporting Bugs

Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md) and include:
- Clear description
- Steps to reproduce
- Expected vs actual behavior
- Environment details

## 💡 Feature Requests

Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md) and include:
- Problem description
- Proposed solution
- Alternatives considered

## 📜 Code of Conduct

- Be respectful and inclusive
- Provide constructive feedback
- Help others learn and grow

## 🙏 Thank You!

Every contribution matters. Thank you for helping improve Smart Attendance System!
