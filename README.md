# Market & Stock Traffic Lights — Google OIDC + My Universe

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
