# Contributing to Self-Healing CCTV Mesh Network

## 1. Development Setup

Clone the repository:

```bash
git clone https://github.com/anshukumar2932/self-healing-cctv-mesh.git
cd self-healing-cctv-mesh
```

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install project dependencies:

```bash
pip install -e .
```

Install development and testing tools:

```bash
pip install ruff pytest pytest-cov
```

## 2. Code Guidelines

- Follow PEP 8 Python coding standards.
- Use meaningful variable and function names.
- Write modular and readable code.
- Add docstrings to important functions and classes.
- Avoid unnecessary changes to existing functionality.
- Handle exceptions appropriately.
- Add test cases for new functionality and bug fixes.

## 3. Linting and Formatting

Run lint checks:

```bash
ruff check .
```

Automatically fix supported lint issues:

```bash
ruff check . --fix
```

Format Python files:

```bash
ruff format .
```

Verify formatting:

```bash
ruff format --check .
```

## 4. Running Tests

Run all test cases:

```bash
pytest -v
```

Run a specific test module:

```bash
pytest tests/test_failure.py -v
```

```bash
pytest tests/test_recovery.py -v
```

```bash
pytest tests/test_storage.py -v
```

Run tests with coverage:

```bash
pytest --cov=src --cov-report=term-missing
```

## 5. Pre-PR Validation

Before raising a Pull Request, run:

```bash
./check.sh
```

Ensure:

- All lint checks pass.
- Code formatting is correct.
- All tests pass.
- No unintended files are included.
- No secrets, credentials, or local configuration files are committed.

## 6. Branching and Pull Requests

Create a feature branch:

```bash
git checkout -b feature/<feature-name>
```

For bug fixes:

```bash
git checkout -b fix/<bug-name>
```

Commit changes:

```bash
git add .
git commit -m "Add <feature-or-fix>"
```

Push your branch:

```bash
git push origin feature/<feature-name>
```

Raise a Pull Request against the `main` branch.

## 7. Pull Request Expectations

Every PR should include:

- A clear description of the change.
- The reason for the change.
- Test cases added or updated.
- Test execution results.
- Any known limitations or follow-up work.

All checks must pass before requesting review.
