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
    subscription_current_period_end timestamptz
);

create table if not exists public.user_symbols (
    user_email text not null references public.app_users(email) on delete cascade,
    symbol text not null,
    created_at timestamptz not null default now(),
    primary key (user_email, symbol)
);

-- The Streamlit server uses the Supabase server secret.
-- Keep that key ONLY in Streamlit Secrets. Never commit it.
