-- Fresh setup for MOJ Market access control and 30-day introductory access.
-- Safe to run in Supabase SQL Editor.

create table if not exists public.app_users (
    email text primary key,
    name text,
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'denied')),
    requested_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    approved_at timestamptz,
    trial_started_at timestamptz,
    trial_ends_at timestamptz,
    subscription_status text not null default 'inactive'
        check (subscription_status in ('inactive', 'trialing', 'active', 'past_due', 'canceled')),
    stripe_customer_id text,
    stripe_subscription_id text,
    subscription_current_period_end timestamptz,
    subscription_cancel_at_period_end boolean not null default false,
    stripe_payment_method_id text,
    card_fingerprint text,
    card_brand text,
    card_last4 text,
    card_fingerprint_reused boolean not null default false,
    normalized_name text,
    review_required boolean not null default false,
    review_reason text,
    review_notified_at timestamptz,
    review_notification_error text,
    trial_eligibility_status text
        check (
            trial_eligibility_status is null
            or trial_eligibility_status in ('eligible', 'reused_card')
        )
);

create table if not exists public.user_symbols (
    user_email text not null references public.app_users(email) on delete cascade,
    symbol text not null,
    created_at timestamptz not null default now(),
    primary key (user_email, symbol)
);

-- The Streamlit server uses the Supabase server secret.
-- Keep that key ONLY in Streamlit Secrets. Never commit it.


-- Approval records approval time only. A free trial starts only after the
-- application verifies an unused Stripe card fingerprint.
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

create index if not exists idx_app_users_normalized_name
    on public.app_users(normalized_name)
    where normalized_name is not null;
