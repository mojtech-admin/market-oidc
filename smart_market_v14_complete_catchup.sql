-- Smart Market v14 COMPLETE catch-up migration
-- Use this if you jumped directly to v14 and skipped one or more prior
-- subscription/fingerprint/trial-gate migrations.
-- Safe for existing app_users rows; does NOT delete users or restart trials.

-- ---------------------------------------------------------------------------
-- Core subscription/trial fields (safe if already present)
-- ---------------------------------------------------------------------------
alter table public.app_users
    add column if not exists approved_at timestamptz,
    add column if not exists trial_started_at timestamptz,
    add column if not exists trial_ends_at timestamptz,
    add column if not exists subscription_status text,
    add column if not exists stripe_customer_id text,
    add column if not exists stripe_subscription_id text,
    add column if not exists subscription_current_period_end timestamptz,
    add column if not exists subscription_cancel_at_period_end boolean not null default false;

update public.app_users
set subscription_status = coalesce(subscription_status, 'inactive')
where subscription_status is null;

alter table public.app_users
    alter column subscription_status set default 'inactive';

alter table public.app_users
    alter column subscription_status set not null;

-- Keep the constraint compatible with all Stripe subscription states used by v14.
alter table public.app_users
    drop constraint if exists app_users_subscription_status_check;

alter table public.app_users
    add constraint app_users_subscription_status_check
    check (
        subscription_status in (
            'inactive',
            'incomplete',
            'incomplete_expired',
            'trialing',
            'active',
            'past_due',
            'canceled',
            'unpaid',
            'paused'
        )
    );

-- ---------------------------------------------------------------------------
-- v12 card-fingerprint fields
-- ---------------------------------------------------------------------------
alter table public.app_users
    add column if not exists stripe_payment_method_id text,
    add column if not exists card_fingerprint text,
    add column if not exists card_brand text,
    add column if not exists card_last4 text,
    add column if not exists card_fingerprint_reused boolean not null default false;

create index if not exists idx_app_users_card_fingerprint
    on public.app_users(card_fingerprint)
    where card_fingerprint is not null;

-- ---------------------------------------------------------------------------
-- v13 payment-method-gated trial field
-- ---------------------------------------------------------------------------
alter table public.app_users
    add column if not exists trial_eligibility_status text;

alter table public.app_users
    drop constraint if exists app_users_trial_eligibility_status_check;

alter table public.app_users
    add constraint app_users_trial_eligibility_status_check
    check (
        trial_eligibility_status is null
        or trial_eligibility_status in ('eligible', 'reused_card')
    );

-- Preserve existing trial history. Do not grant a fresh trial here.
update public.app_users
set trial_eligibility_status = case
    when card_fingerprint_reused is true then 'reused_card'
    when trial_started_at is not null then 'eligible'
    else trial_eligibility_status
end
where trial_eligibility_status is null;

-- ---------------------------------------------------------------------------
-- v14 signup-risk/review fields
-- ---------------------------------------------------------------------------
alter table public.app_users
    add column if not exists normalized_name text,
    add column if not exists review_required boolean not null default false,
    add column if not exists review_reason text,
    add column if not exists review_notified_at timestamptz,
    add column if not exists review_notification_error text;

create index if not exists idx_app_users_normalized_name
    on public.app_users(normalized_name)
    where normalized_name is not null;

-- Backfill normalized names for existing rows.
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

-- ---------------------------------------------------------------------------
-- IMPORTANT: approval no longer starts the trial.
-- Approval only records approved_at. The app starts the 30-day trial only
-- after Stripe payment-method collection and fingerprint eligibility check.
-- ---------------------------------------------------------------------------
create or replace function public.set_app_user_access_dates()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();

    if new.status = 'approved'
       and (tg_op = 'INSERT' or old.status is distinct from 'approved') then
        new.approved_at := coalesce(new.approved_at, now());
    end if;

    return new;
end;
$$;

drop trigger if exists trg_set_app_user_access_dates on public.app_users;

create trigger trg_set_app_user_access_dates
before insert or update of status on public.app_users
for each row execute function public.set_app_user_access_dates();
