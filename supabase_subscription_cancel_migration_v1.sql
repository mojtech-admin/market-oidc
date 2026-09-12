-- Smart Market subscription cancellation-state migration v1
-- Run once in Supabase SQL Editor after the 30-day trial migration.

alter table public.app_users
    add column if not exists subscription_cancel_at_period_end boolean not null default false;
