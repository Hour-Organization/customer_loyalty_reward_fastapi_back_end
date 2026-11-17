# KhmerShop API (FastAPI)

The backend for the KhmerShop customer-loyalty e-commerce demo: catalog, cash-on-delivery orders, a points
ledger, membership tiers, badges, referrals, campaigns, rewards with their own stock ledger, and role-based
staff access.

It is a port of the Laravel API in `customer_loyalty_ecommerce_api` (branch `main`) and keeps its contract:
the same routes under `/api`, the same JSON shapes (decimals as strings such as `"1.50"`), the same validation
messages, and Laravel's error format `{"message": "...", "errors": {"field": ["..."]}}`. The frontend in
`customer_loyalty_reward_fastapi_front_end` works against it unchanged.

## Run it

Python 3.12 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env               # SQLite by default
.venv/bin/python -m app.cli migrate
.venv/bin/python -m app.cli seed
.venv/bin/uvicorn app.main:app --reload --port 8000
```

The interactive API docs are at http://localhost:8000/docs.

To point the frontend at this server, start it with `VITE_API_URL=http://localhost:8000/api pnpm dev`.

Demo accounts (all with the password `password`):

| Who | Email | Signs in at |
| --- | --- | --- |
| Super Admin | `test@example.com` | `POST /api/backend/login` |
| Customer | `customer@example.com` | `POST /api/login` |

### With Docker and PostgreSQL

```bash
docker compose up --build
docker compose exec api python -m app.cli seed
```

## Commands

| Command | What it does |
| --- | --- |
| `python -m app.cli migrate` | Apply the Alembic migrations |
| `python -m app.cli seed` | Add the demo data (safe to run again) |
| `python -m app.cli fresh` | Drop every table, migrate and seed |
| `python -m app.cli expire-redemptions` | Refund reservations whose time ran out |
| `pytest` | Run the tests (in-memory SQLite) |
| `ruff check app tests` | Lint |

The server also expires overdue reservations itself every 5 minutes (`EXPIRE_REDEMPTIONS_EVERY`), as the
Laravel scheduler did.

After changing a model, add a migration with
`alembic revision --autogenerate -m "what changed"` and check the generated file.

## Layout

```
app/
  main.py          the app: routers under /api, uploads under /storage, error handlers, expiry task
  config.py        settings from the environment or .env
  database.py      engine, a session per request, soft-delete filtering
  models.py        every table, with the to_array()/to_api() shapes the API returns
  validation.py    Laravel-style rules and messages
  http.py          request input (JSON and PHP-style multipart), responses, pagination, attempt()
  auth.py          passwords, Sanctum-style tokens, the staff and customer guards, page and stock gates
  services/        business rules: loyalty, orders, product_stock, stock_ledger, redemptions, records
  routers/         one module per area of the API
  seeders.py       the demo data
  cli.py           the commands above
alembic/           migrations
tests/             pytest suite
```

## How it works

- **Two kinds of account.** Staff (`users`) sign in under `/api/backend`, customers at `/api`. Tokens look
  like Sanctum's (`<id>|<secret>`, stored as a SHA-256) and only open their own side. A token unused for
  `TOKEN_IDLE_TIMEOUT` minutes (7 days by default) stops working.
- **Page access.** A staff role lists the admin pages it opens; Super Admin opens every page. Roles that open
  an inventory page also have a stock authority (`clerk`, `manager`, or `admin` for Super Admin), which the
  stock routes check.
- **Points.** Every change to a balance is a `loyalty_transactions` row with the balance before and after.
  Completing an order pays base points × product bonus × quantity × tier multiplier × the best live
  campaign. Earned points count towards the tier unless staff locked it. The first completed order pays the
  referral bonuses (100 to the referrer, 50 to the friend), and badges are awarded when a threshold is met.
- **Reward stock.** Levels are never stored: they are the sum of `reward_stock_movements`, per reward,
  variant and location, with batches (first expiry, first out) and serial numbers. Redeeming reserves a unit
  at the best-stocked location or backorders it. Stock arriving fills backorders oldest first and flags
  restock requests.
- **Multipart updates.** Browsers send product, reward and campaign updates as a multipart `POST` with
  `_method=PATCH`; those routes accept that as well as `PATCH` and `PUT`.
