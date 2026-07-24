# Developing SilentRelay

SilentRelay requires Python 3.12 or newer.

## Local setup

Create a virtual environment and install the project with its test
dependencies:

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[test]"
```

For local HTTP development only, set:

```text
APP_ENV=test
APP_BASE_URL=http://localhost:8000
SECURE_COOKIES=false
HSTS_ENABLED=false
```

Do not use these values in production.

Initialize the database and start the application:

```sh
.venv/Scripts/alembic upgrade head
.venv/Scripts/uvicorn app.main:app --reload --no-access-log
```

Run the scheduler in a second terminal:

```sh
.venv/Scripts/python -m app.scheduler.main
```

## Tests

Run the complete test suite:

```sh
.venv/Scripts/pytest
```

The tests use isolated configuration and do not require a production SMTP
account.
