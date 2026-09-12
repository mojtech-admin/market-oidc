-- Smart Market v13: payment-method-gated free trial.
-- Run ONCE before deploying v13.
--
-- New workflow:
-- Google sign-in -> admin approval -> Stripe payment-method verification
-- -> fingerprint check -> start 30-day trial only if fingerprint is unused.

alter table public.app_users
    add column if not exists trial_eligibility_status text;

update public.app_users
set trial_eligibility_status = case
    when card_fingerprint_reused is true then 'reused_card'
    when trial_started_at is not null then 'eligible'
    else null
end
where trial_eligibility_status is null;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'app_users_trial_eligibility_status_check'
    ) then
        alter table public.app_users
        add constraint app_users_trial_eligibility_status_check
        check (
            trial_eligibility_status is null
            or trial_eligibility_status in ('eligible', 'reused_card')
        );
    end if;
end $$;

-- Replace the old approval trigger behavior. Approval now records only approved_at.
-- Trial dates are created by the application only after Stripe verifies an unused card.
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
