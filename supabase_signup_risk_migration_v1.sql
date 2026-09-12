-- Smart Market v14: automated signup review flags and admin email workflow.
-- Run ONCE before deploying v14.

alter table public.app_users
    add column if not exists normalized_name text,
    add column if not exists review_required boolean not null default false,
    add column if not exists review_reason text,
    add column if not exists review_notified_at timestamptz,
    add column if not exists review_notification_error text;

create index if not exists idx_app_users_normalized_name
    on public.app_users(normalized_name)
    where normalized_name is not null;

-- Backfill existing names using the same broad normalization intent used by
-- the application: lowercase, punctuation collapsed to spaces, whitespace trimmed.
update public.app_users
set normalized_name = trim(
    regexp_replace(
        regexp_replace(lower(coalesce(name, '')), '[^a-z0-9]+', ' ', 'g'),
        '\s+', ' ', 'g'
    )
)
where normalized_name is null
  and name is not null
  and trim(name) <> '';
