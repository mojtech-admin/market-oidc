# Smart Market — Google OIDC + My Universe

## What changed

- Google OIDC is the single sign-in layer.
- Streamlit Sharing should NOT also be used as a private email gate, to avoid double sign-in.
- First-time Google users are recorded in Supabase as `pending`.
- Administrator approves or denies them by changing `app_users.status` in Supabase.
- Approved users can save custom symbols to **My Universe**.
- Saved symbols follow the same verified Google account across browsers/devices.
- Proprietary weighting values are not stored in the Python source or repository.
- The app remains dark-themed.
- UI labels are **Y Method** and **Smart Proprietary Method**.
- The visible weighted reason/score is removed.
- Footer includes `Questions? mojconsulting@gmail.com`.

## 1. Streamlit Sharing setting

To avoid two authentication screens, make the Community Cloud app reachable
without Streamlit's separate viewer-email gate. The application itself blocks
all dashboard access until Google OIDC login and server-side approval succeed.

## 2. Google OIDC

Create a Google OAuth/OIDC Web application.

Authorized redirect URI:

`https://YOUR-APP.streamlit.app/oauth2callback`

Then configure `[auth]` in Streamlit Community Cloud -> App settings -> Secrets.
See `.streamlit/secrets.toml.example` for the required key names.

Do not commit the real client secret or cookie secret.

## 3. Supabase

Create a Supabase project, then run `supabase_setup.sql` in the Supabase SQL Editor.

Put the project URL and **service role key** in Streamlit Secrets under `[supabase]`.
Never expose or commit the service-role key.

### Approve or deny a user

After a new user signs in with Google, a row appears in `app_users`:

- `pending` — user cannot enter yet
- `approved` — user can enter
- `denied` — user is blocked

Change the `status` field in the Supabase Table Editor.

The user can click **Check access again** after approval.

## 4. Proprietary weights

The exact numerical weighting profiles and thresholds are intentionally absent
from the repository. Configure them only in Streamlit server-side Secrets under
`[proprietary]`.

Do not put real proprietary values in `secrets.toml.example` or any Git-tracked file.

## 5. My Universe

The global default universe remains unchanged.

When an approved user enters another valid ticker, a **Save <SYMBOL> to My Universe**
button is available. Saved symbols are stored in `user_symbols` under that user's
verified Google email and are included in **Current universe** on future visits.

## 6. Deployment files

- `streamlit_app.py`
- `traffic_lights_core.py`
- `requirements.txt`
- `.streamlit/config.toml`
- `supabase_setup.sql`
- `.gitignore`

Do not deploy/commit a real `.streamlit/secrets.toml`.


## 7. Stripe test subscription integration

Configure Streamlit Secrets:

```toml
[stripe]
secret_key = "sk_test_..."
price_id = "price_..."
app_url = "https://mojmarket.streamlit.app"
```

The app creates a Stripe-hosted Checkout Session for the authenticated user's email.
After successful checkout, Stripe returns to the app with the Checkout Session ID.
The app verifies that session server-side using the Stripe secret key, stores the
customer/subscription identifiers and current subscription status in Supabase, and
then grants access. On later visits, the app re-checks the Stripe subscription by ID
and refreshes Supabase.

This version does not require a public webhook endpoint for basic access control.
A webhook can still be added later for real-time background updates when users are
not actively visiting the app.

## Stripe checkout UI v2
The subscription CTA is capped at 620px on desktop and remains responsive/full-width on narrow/mobile screens. No payment-flow logic was changed.


## Stripe checkout v4
Subscribed users now have a Cancel subscription control in the sidebar. Cancellation is scheduled at the end of the current Stripe billing period so access continues through the paid period.


## Subscription state handling (v7)

The app handles trial, active, cancellation-at-period-end, resumed, expired/canceled,
and Stripe payment-recovery states. Stripe statuses `past_due`, `unpaid`, and
`incomplete` are routed to a Stripe Customer Portal payment-recovery screen instead
of the market dashboard. No database migration is required beyond the included
30-day trial and cancellation migrations.

For production, Stripe webhooks are still recommended so Supabase is updated even
when a user is not actively visiting the app.


## Stripe webhook (v8)

This version adds a Supabase Edge Function at:

`supabase/functions/stripe-webhook/index.ts`

and function configuration at:

`supabase/config.toml`

The webhook verifies Stripe's signature and synchronizes Stripe subscription
state into `public.app_users`. It handles:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`

Before enabling the webhook, run:

`supabase_subscription_status_migration_v2.sql`

This expands the database constraint to accept Stripe states including
`incomplete`, `unpaid`, and `paused`.

The Edge Function requires two project secrets:

- `STRIPE_SECRET_KEY` — the same Stripe sandbox/live secret key used by the app
- `STRIPE_WEBHOOK_SIGNING_SECRET` — the `whsec_...` secret from the Stripe webhook endpoint

The function itself must be publicly callable (`verify_jwt = false`) because
Stripe does not send a Supabase user JWT. Authenticity is enforced by verifying
the `Stripe-Signature` header against the signing secret.


## v9 — Stripe current billing-period compatibility

Stripe API versions from Basil onward moved `current_period_end` from the top-level
Subscription object to each subscription item. v9 reads the billing-period end from
`subscription.items.data[0].current_period_end`, with a legacy fallback for older
Stripe API versions. This fix is applied both to the Streamlit Stripe sync and the
Supabase `stripe-webhook` Edge Function. No new Supabase migration is required.



## v10 — Fix Streamlit sync overwriting billing-period end

v9 added the modern Stripe item-level billing-period helper, but one Streamlit
mapping function still read the removed top-level `subscription.current_period_end`.
That caused a correctly populated webhook value to be overwritten with NULL whenever
the app synchronized the subscription from Stripe. v10 routes that mapping through
the item-level helper as intended. No event-list changes, secret changes, or database
migration are required.




## v11 — pre-test reliability review

v11 keeps the existing subscription state model and webhook event list, while
hardening several implementation details before end-to-end testing:

- billing-period lookup now prefers Stripe `cancel_at` for scheduled
  cancellations, then item-level `current_period_end`, and can retrieve the
  subscription item directly if Stripe returns a thin object;
- the same defensive period lookup is used by the Supabase webhook;
- Stripe Checkout reuses an existing Stripe customer when available instead
  of creating unnecessary duplicate customers;
- cached Checkout URLs are scoped to the signed-in email and cleared after a
  successful checkout;
- obsolete local signup-JSON code and generated Python cache files were
  removed from the package.

No new Supabase migration, Stripe event selection, or Streamlit secret is
required for v11.



## v12 — Stripe card fingerprint metadata

v12 adds server-side capture of Stripe PaymentMethod card metadata:
- `stripe_payment_method_id`
- `card_fingerprint`
- `card_brand`
- `card_last4`
- `card_fingerprint_reused`

The raw card number, expiration date, and CVC never enter Smart Market or Supabase.

Run `supabase_card_fingerprint_migration_v1.sql` once before deploying v12.

Important: Smart Market's current trial starts before a payment method is collected.
Therefore fingerprint reuse can be detected after Stripe Checkout, but it cannot yet
prevent a second Gmail account from consuming a new free trial. To enforce one free
trial per card, move payment-method collection to the beginning of the trial flow.


## v13 — payment-method-gated 30-day free trial

New-user access flow:

1. Google sign-in
2. Administrator approval
3. Stripe-hosted payment-method verification (`mode="setup"`)
4. Stripe card fingerprint check
5. If the fingerprint has not previously received a Smart Market trial:
   - start the 30-day trial
   - do not charge the card
6. If the fingerprint has previously received a trial:
   - do not start another trial
   - offer the normal paid subscription

The pre-trial screen explicitly tells the user:

> You will not be charged during the 30-day free trial.

The card is collected by Stripe. Smart Market never receives or stores the full card
number, expiration date, or CVC.

### Required deployment steps

1. Run `supabase_trial_payment_gate_migration_v1.sql` once.
2. Deploy the v13 Streamlit files.
3. Replace/redeploy `supabase/functions/stripe-webhook/index.ts`.
4. Keep the existing Stripe webhook event list; `checkout.session.completed` now
   handles both subscription Checkout and payment-method setup Checkout.

Existing users with already-populated trial dates or active subscriptions retain
their current access state.


## v14 — automated signup review + repeat-trial controls

New signup behavior:

- The existing `status` field remains unchanged: `pending`, `approved`, or `denied`.
- Same Google email always maps to the existing `app_users` row, so the same email
  cannot create another trial.
- Low-risk new users are automatically created as `approved`.
- A duplicate normalized Google profile name is flagged for manual review.
- An unusually dense signup burst (5 or more prior requests in 10 minutes) is also
  flagged for review.
- Flagged users remain `pending` with:
  - `review_required = true`
  - `review_reason = ...`
- If Resend alerts are configured, the administrator receives an email immediately.
- After manual approval, the existing v13 Stripe payment-method gate runs:
  - unused card fingerprint -> 30-day free trial
  - previously used trial card -> no second free trial, offer paid subscription

### Required deployment

1. Run `supabase_signup_risk_migration_v1.sql` once.
2. Add `[alerts]` secrets in Streamlit if email notifications are desired.
3. Deploy the v14 Streamlit files.
4. No new Stripe webhook events are required.
5. No Supabase Edge Function code change is required specifically for v14.

### Email alerts with Resend

Add to Streamlit Secrets:

```toml
[alerts]
resend_api_key = "re_..."
admin_email = "YOUR_ADMIN_EMAIL"
from_email = "Smart Market <onboarding@resend.dev>"
```

For production email sending, verify a domain in Resend and replace `from_email`
with an address on that verified domain.


## v15 messaging refinement

The new-user flow still performs email/name checks before payment-method verification.
The pre-trial payment page now uses concise copy and no longer mentions "free-trial abuse."

Current wording:
- `Start your 30-day free trial`
- `Verify a payment method to confirm you are a real user. You will not be charged during the 30-day free trial.`
- `Payment details are handled securely by Stripe.`
