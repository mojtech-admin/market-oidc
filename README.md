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

