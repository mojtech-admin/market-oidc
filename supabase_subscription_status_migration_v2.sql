-- Smart Market Stripe subscription-status migration v2
-- Run once in Supabase SQL Editor before enabling Stripe webhooks.
-- Expands the app_users subscription_status constraint to all current Stripe
-- subscription states that Smart Market may receive from Stripe.

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
