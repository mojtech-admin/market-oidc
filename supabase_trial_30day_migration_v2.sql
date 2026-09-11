-- MOJ Market 30-day trial migration v2
-- Run ONCE in Supabase SQL Editor for an existing project that has not already run the 30-day trial migration.
-- Adds 30-day introductory-access and Stripe subscription fields.

alter table public.app_users add column if not exists approved_at timestamptz;
alter table public.app_users add column if not exists trial_started_at timestamptz;
alter table public.app_users add column if not exists trial_ends_at timestamptz;
alter table public.app_users add column if not exists subscription_status text;
alter table public.app_users add column if not exists stripe_customer_id text;
alter table public.app_users add column if not exists stripe_subscription_id text;
alter table public.app_users add column if not exists subscription_current_period_end timestamptz;

update public.app_users
set subscription_status = coalesce(subscription_status, 'inactive')
where subscription_status is null;

alter table public.app_users
    alter column subscription_status set default 'inactive';

alter table public.app_users
    alter column subscription_status set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'app_users_subscription_status_check'
    ) then
        alter table public.app_users
        add constraint app_users_subscription_status_check
        check (subscription_status in ('inactive', 'trialing', 'active', 'past_due', 'canceled'));
    end if;
end $$;

create or replace function public.set_app_user_access_dates()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();

    if new.status = 'approved' and (tg_op = 'INSERT' or old.status is distinct from 'approved') then
        new.approved_at := coalesce(new.approved_at, now());
        new.trial_started_at := coalesce(new.trial_started_at, now());
        new.trial_ends_at := coalesce(new.trial_ends_at, now() + interval '30 days');
    end if;

    return new;
end;
$$;

drop trigger if exists trg_set_app_user_access_dates on public.app_users;
create trigger trg_set_app_user_access_dates
before insert or update of status on public.app_users
for each row execute function public.set_app_user_access_dates();

-- Existing approved users have no historical approval timestamp.
-- Give them a fresh 30-day period starting when this migration is run.
update public.app_users
set approved_at = coalesce(approved_at, now()),
    trial_started_at = coalesce(trial_started_at, now()),
    trial_ends_at = coalesce(trial_ends_at, now() + interval '30 days')
where status = 'approved'
  and trial_ends_at is null;
