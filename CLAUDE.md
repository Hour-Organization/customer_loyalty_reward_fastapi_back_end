# CLAUDE.md

FastAPI port of the Laravel API in `../customer_loyalty_ecommerce_api` (branch `main`). The frontend
`../customer_loyalty_reward_fastapi_front_end` (see its `src/data/api.ts`) is the client, so **the HTTP
contract is fixed**: routes, JSON field names and types, status codes, and validation messages must stay as
the Laravel API had them. When in doubt, read the Laravel controller, form request or model it came from.

## Commands

- Tests: `.venv/bin/pytest` (in-memory SQLite; one file: `.venv/bin/pytest tests/test_stock.py`)
- Lint: `.venv/bin/ruff check app tests`
- Run: `.venv/bin/uvicorn app.main:app --reload --port 8000` (docs at `/docs`)
- Data: `.venv/bin/python -m app.cli migrate | seed | fresh | expire-redemptions`
- New migration after a model change: `.venv/bin/alembic revision --autogenerate -m "..."`

## Conventions

- Business rules live in `app/services/`; routers validate, call a service, commit and shape the response.
- Handlers are sync, use `Db`/`Input` from `app.auth`/`app.http`, and are wrapped in
  `@attempt("Could not …")` so unexpected errors become a JSON 500. Handlers commit explicitly; anything
  left uncommitted is rolled back when the request ends.
- Validate with `app.validation.validate(data, rules, messages)` using Laravel rule strings (`"required"`,
  `"max:255"`, `"sometimes"`, `"nullable"`) and `In`/`Exists`/`Unique`/`Regex`/`RequiredIf` or a callable
  `(attribute, value, fail)`. Raise `ValidationError({field: [message]})` (or `validation.fail`) for a 422.
  Use `refuse(message)` for the `{"success": false, "message"}` 422s the Laravel controllers returned.
- Input is already trimmed with empty strings turned into `None`, as Laravel's middleware did.
- Responses: `ok(data, message, status)`, `done(message)`, `page_of(...)` for paginated lists
  (`data` + `meta`). Shape rows with `model.to_array()` (Eloquent's `toArray()`, decimals as strings,
  timestamps as `...000000Z`) or `model.to_api()` (the Laravel `toApiArray()`, timestamps as `+00:00`).
- Staff routes: guard with `Annotated[User, pages("page")]`, `SuperAdmin`, `StockStaff` or
  `stock_authority("manager")`. Changes made through the Laravel `RecordStaffActivity` routes call
  `log_staff_change(db, staff, "SUBJECT", "ACTION", name)` before committing.
- Update routes that take multipart use `@update_route(router, path)` (PUT, PATCH and the `_method` POST).
- `categories`, `products` and `roles` are soft-deleted: queries hide them unless they pass
  `.execution_options(include_deleted=True)`.
- Use `app.clock.now()`/`today()` (naive UTC), never `datetime.now()`, so tests can move time with
  `clock.travel`.
- Tests build data with `tests/factories.py` (`make.staff(pages)`, `make.customer()`, `make.reward()`,
  `make.receive(...)`) and sign in with `make.headers(user)`. Call `db.expire_all()` before reading rows the
  API changed.
