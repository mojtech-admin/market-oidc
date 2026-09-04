-- Run this once in the Supabase SQL Editor.

create table if not exists public.app_users (
    email text primary key,
    name text,
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'denied')),
    requested_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.user_symbols (
    user_email text not null references public.app_users(email) on delete cascade,
    symbol text not null,
    created_at timestamptz not null default now(),
    primary key (user_email, symbol)
);

-- The Streamlit server uses the Supabase service-role key.
-- Keep that key ONLY in Streamlit Secrets. Never commit it.
