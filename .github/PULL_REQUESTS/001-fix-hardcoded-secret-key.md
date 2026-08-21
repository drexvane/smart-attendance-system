# PR #001: Fix Security Issue - Remove Hardcoded Secret Key

> **Superseded by the backend refactor.** The concrete change described here landed against
> the old `config.py` and a `get_secret_key()` helper. The current equivalent is
> `ProductionConfig.validate()` in `app/config.py`, which raises if `SECRET_KEY` is unset;
> `DevelopmentConfig` generates a random key. The entrypoint is now `wsgi.py` / `run.py`, not
> `app.py`. The "How to Test" block below has been updated accordingly; the rest is kept as
> the original record.

## Description
This PR fixes a security vulnerability where a hardcoded fallback secret key was used in `config.py`.

## Related Issue
Fixes #001 - Security - Hardcoded Secret Key Fallback

## Type of Change
- [x] Bug fix
- [x] Security fix
- [ ] New feature
- [ ] Breaking change
- [x] Documentation update

## Changes Made
1. **config.py**: Added `get_secret_key()` function that:
   - Uses `SECRET_KEY` from environment if available
   - Raises error in production if `SECRET_KEY` not set
   - Generates secure random key for development (with warning)

2. **Added `.env.example`**: Template for environment variables

3. **Added `.gitignore`**: Prevents committing sensitive files

## Testing
- [x] Tested locally in development mode (auto-generates key)
- [x] Verified error is raised when `FLASK_ENV=production` without `SECRET_KEY`
- [x] Application starts normally with `SECRET_KEY` set

## Security Checklist
- [x] No hardcoded secrets
- [x] Sensitive files in `.gitignore`
- [x] Clear documentation for setup

## How to Test
```bash
# Development (should work, auto-generates a key with a warning)
python run.py

# Production without SECRET_KEY (should fail at startup)
FLASK_ENV=production gunicorn -c gunicorn.conf.py wsgi:app

# Production with SECRET_KEY (should work)
SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  FLASK_ENV=production gunicorn -c gunicorn.conf.py wsgi:app
```
