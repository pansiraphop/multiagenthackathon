# Database

Postgres on Supabase, 9 tables (README §3). **Prisma is the schema source of truth and
nothing else** — there is no Prisma client, no JS. The Python app reads and writes at
runtime through `supabase-py`, so every default and constraint lives in the database
itself, not in an ORM layer.

## Create the tables — two ways

**A. Paste the SQL (no setup, do this first).**
Open `sql/schema.sql`, copy all of it, paste into the Supabase dashboard →
**SQL Editor** → Run. It is idempotent-ish at the top (`CREATE EXTENSION IF NOT EXISTS
pgcrypto`) but `CREATE TABLE` is not — run it once, on an empty database.

**B. Let Prisma push it.**
```bash
cp .env.example .env     # fill in DATABASE_URL and DIRECT_URL
npm install
npm run db:push
```

## The two connection strings

Supabase dashboard → **Project Settings → Database → Connection string** (URI tab).
Same credentials, two ports:

| Env var | Port | Which one |
|---|---|---|
| `DATABASE_URL` | `6543` | Pooled / "Transaction pooler" (pgBouncer) |
| `DIRECT_URL`   | `5432` | Direct / "Session pooler" |

Prisma needs both. DDL cannot run through pgBouncer's transaction pooling, so
`db:push` uses `DIRECT_URL`. URL-encode special characters in the password.

The Python app does **not** use these — it uses `SUPABASE_URL` +
`SUPABASE_SERVICE_KEY` (Project Settings → API).

## Changing the schema

```bash
# 1. edit prisma/schema.prisma
npm run db:validate     # syntax + relations  (needs the env vars set)
npm run sql:generate    # rewrites sql/schema.sql  (needs NO database, no creds)
npm run db:push         # apply to Supabase, or paste the new SQL by hand
```

Commit `prisma/schema.prisma` and `sql/schema.sql` together, and tell the other person —
a schema change is a contract change (README §3).

Two things to keep in mind when editing:

- **Defaults must reach the database.** UUID keys use
  `@default(dbgenerated("gen_random_uuid()"))` and timestamps use `@default(now())`
  precisely because a Prisma-client-only default would be invisible to `supabase-py`
  and would insert nulls.
- **Names are snake_case on purpose.** Python queries by literal column name. Models
  carry `@@map` to the snake_case table name; field names are already snake_case.

`npm run db:studio` opens a browser table browser if you want to eyeball rows.
