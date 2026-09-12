-- Smart Market v12: store Stripe payment-method fingerprint metadata.
-- No raw card number, expiry, or CVC is stored.

alter table public.app_users
    add column if not exists stripe_payment_method_id text,
    add column if not exists card_fingerprint text,
    add column if not exists card_brand text,
    add column if not exists card_last4 text,
    add column if not exists card_fingerprint_reused boolean not null default false;

create index if not exists idx_app_users_card_fingerprint
    on public.app_users(card_fingerprint)
    where card_fingerprint is not null;
