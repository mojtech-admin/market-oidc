import Stripe from 'npm:stripe@^22'
import { withSupabase } from 'npm:@supabase/server@^1'

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') as string)
const cryptoProvider = Stripe.createSubtleCryptoProvider()


async function getSubscriptionCurrentPeriodEnd(subscription: any): Promise<number | null> {
  // If Stripe has resolved a scheduled cancellation timestamp, prefer it for
  // the user's access-until date.
  if (subscription?.cancel_at_period_end && subscription?.cancel_at) {
    return subscription.cancel_at
  }

  // Stripe API versions from Basil onward expose current_period_end on
  // subscription items instead of the top-level Subscription object.
  const firstItem = subscription?.items?.data?.[0]
  if (firstItem?.current_period_end) {
    return firstItem.current_period_end
  }

  // Defensive fallback for thin webhook objects: retrieve the subscription
  // item directly from Stripe.
  if (firstItem?.id) {
    try {
      const item = await stripe.subscriptionItems.retrieve(firstItem.id)
      if ((item as any)?.current_period_end) {
        return (item as any).current_period_end
      }
    } catch (err) {
      console.warn('Stripe webhook: could not retrieve subscription item period', err)
    }
  }

  // Backward-compatible fallback for older Stripe API versions.
  return subscription?.current_period_end ?? null
}

function isoFromUnix(value: number | null | undefined): string | null {
  if (!value) return null
  return new Date(value * 1000).toISOString()
}

function stringId(value: unknown): string {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'object' && value !== null && 'id' in value) {
    return String((value as { id?: unknown }).id ?? '')
  }
  return ''
}

function subscriptionIdFromInvoice(invoice: Stripe.Invoice): string {
  const legacy = stringId((invoice as unknown as { subscription?: unknown }).subscription)
  if (legacy) return legacy

  const parent = (invoice as unknown as {
    parent?: { subscription_details?: { subscription?: unknown } }
  }).parent
  return stringId(parent?.subscription_details?.subscription)
}

async function findEmailForSubscription(
  subscription: Stripe.Subscription,
  supabaseAdmin: any,
): Promise<string> {
  const metadataEmail = String(subscription.metadata?.user_email ?? '').trim().toLowerCase()
  if (metadataEmail) return metadataEmail

  const subId = subscription.id
  const customerId = stringId(subscription.customer)

  if (subId) {
    const { data } = await supabaseAdmin
      .from('app_users')
      .select('email')
      .eq('stripe_subscription_id', subId)
      .maybeSingle()
    if (data?.email) return String(data.email).trim().toLowerCase()
  }

  if (customerId) {
    const { data } = await supabaseAdmin
      .from('app_users')
      .select('email')
      .eq('stripe_customer_id', customerId)
      .maybeSingle()
    if (data?.email) return String(data.email).trim().toLowerCase()
  }

  if (customerId) {
    try {
      const customer = await stripe.customers.retrieve(customerId)
      if (!('deleted' in customer) || !customer.deleted) {
        const email = String(customer.email ?? '').trim().toLowerCase()
        if (email) return email
      }
    } catch (_) {
      // Fall through and return an empty email.
    }
  }

  return ''
}


async function findCardFingerprintReuse(
  email: string,
  fingerprint: string,
  supabaseAdmin: any,
): Promise<boolean> {
  if (!fingerprint) return false

  const { data, error } = await supabaseAdmin
    .from('app_users')
    .select('email')
    .eq('card_fingerprint', fingerprint)
    .neq('email', email)
    .limit(1)

  if (error) throw error
  return Boolean(data?.length)
}

async function paymentMethodFieldsForCustomer(
  customerId: string,
  email: string,
  supabaseAdmin: any,
): Promise<Record<string, unknown>> {
  if (!customerId) return {}

  let paymentMethod: Stripe.PaymentMethod | null = null

  try {
    const customer = await stripe.customers.retrieve(customerId)
    if (!('deleted' in customer) || !customer.deleted) {
      const defaultPm = stringId(customer.invoice_settings?.default_payment_method)
      if (defaultPm) {
        paymentMethod = await stripe.paymentMethods.retrieve(defaultPm)
      }
    }
  } catch (_) {
    paymentMethod = null
  }

  if (!paymentMethod) {
    const methods = await stripe.paymentMethods.list({
      customer: customerId,
      type: 'card',
      limit: 10,
    })
    const sorted = [...methods.data].sort(
      (a, b) => Number(b.created ?? 0) - Number(a.created ?? 0),
    )
    paymentMethod = sorted[0] ?? null
  }

  if (!paymentMethod?.card) return {}

  const fingerprint = String(paymentMethod.card.fingerprint ?? '').trim()

  return {
    stripe_payment_method_id: paymentMethod.id,
    card_fingerprint: fingerprint || null,
    card_brand: paymentMethod.card.brand ?? null,
    card_last4: paymentMethod.card.last4 ?? null,
    card_fingerprint_reused: fingerprint
      ? await findCardFingerprintReuse(email, fingerprint, supabaseAdmin)
      : false,
  }
}


async function paymentMethodFieldsFromId(
  paymentMethodId: string,
  email: string,
  supabaseAdmin: any,
): Promise<Record<string, unknown>> {
  if (!paymentMethodId) return {}

  const paymentMethod = await stripe.paymentMethods.retrieve(paymentMethodId)
  if (!paymentMethod.card) {
    throw new Error('Trial payment method is not a supported card')
  }

  const fingerprint = String(paymentMethod.card.fingerprint ?? '').trim()

  const { data, error } = await supabaseAdmin
    .from('app_users')
    .select('email')
    .eq('card_fingerprint', fingerprint)
    .neq('email', email)
    .not('trial_started_at', 'is', null)
    .limit(1)

  if (error) throw error
  const reused = Boolean(data?.length)

  return {
    stripe_payment_method_id: paymentMethod.id,
    card_fingerprint: fingerprint || null,
    card_brand: paymentMethod.card.brand ?? null,
    card_last4: paymentMethod.card.last4 ?? null,
    card_fingerprint_reused: reused,
  }
}

async function processTrialSetupSession(
  session: Stripe.Checkout.Session,
  supabaseAdmin: any,
) {
  const email = String(
    session.client_reference_id ||
    session.metadata?.user_email ||
    session.customer_details?.email ||
    '',
  ).trim().toLowerCase()

  if (!email) return

  const setupIntentId = stringId(session.setup_intent)
  if (!setupIntentId) return

  const setupIntent = await stripe.setupIntents.retrieve(setupIntentId)
  const paymentMethodId = stringId(setupIntent.payment_method)
  if (!paymentMethodId) return

  const fields = await paymentMethodFieldsFromId(
    paymentMethodId,
    email,
    supabaseAdmin,
  )
  const reused = Boolean(fields.card_fingerprint_reused)

  const payload: Record<string, unknown> = {
    ...fields,
    stripe_customer_id: stringId(session.customer) || null,
    trial_eligibility_status: reused ? 'reused_card' : 'eligible',
  }

  if (!reused) {
    const now = new Date()
    const trialEnd = new Date(now.getTime() + 30 * 24 * 60 * 60 * 1000)
    payload.trial_started_at = now.toISOString()
    payload.trial_ends_at = trialEnd.toISOString()
  } else {
    payload.trial_started_at = null
    payload.trial_ends_at = null
  }

  const { error } = await supabaseAdmin
    .from('app_users')
    .update(payload)
    .eq('email', email)

  if (error) throw error
}

async function persistSubscription(
  subscription: Stripe.Subscription,
  supabaseAdmin: any,
  emailHint = '',
) {
  const email = (emailHint || await findEmailForSubscription(subscription, supabaseAdmin))
    .trim()
    .toLowerCase()

  if (!email) {
    console.warn('Stripe webhook: unable to map subscription to app user', subscription.id)
    return
  }

  const customerId = stringId(subscription.customer)
  const paymentFields = await paymentMethodFieldsForCustomer(
    customerId,
    email,
    supabaseAdmin,
  )

  const payload = {
    subscription_status: subscription.status,
    stripe_subscription_id: subscription.id,
    stripe_customer_id: customerId,
    subscription_current_period_end: isoFromUnix(
      await getSubscriptionCurrentPeriodEnd(subscription),
    ),
    subscription_cancel_at_period_end: Boolean(subscription.cancel_at_period_end),
    ...paymentFields,
  }

  const { error } = await supabaseAdmin
    .from('app_users')
    .update(payload)
    .eq('email', email)

  if (error) throw error
}

async function syncSubscriptionById(
  subscriptionId: string,
  supabaseAdmin: any,
  emailHint = '',
) {
  if (!subscriptionId) return
  const subscription = await stripe.subscriptions.retrieve(subscriptionId)
  await persistSubscription(subscription, supabaseAdmin, emailHint)
}

export default {
  fetch: withSupabase({ auth: 'none' }, async (req, ctx) => {
    if (req.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 })
    }

    const signature = req.headers.get('stripe-signature') ?? ''
    const body = await req.text()

    let event: Stripe.Event
    try {
      event = await stripe.webhooks.constructEventAsync(
        body,
        signature,
        Deno.env.get('STRIPE_WEBHOOK_SIGNING_SECRET')!,
        undefined,
        cryptoProvider,
      )
    } catch (err) {
      console.error('Stripe signature verification failed:', err)
      return new Response('Bad signature', { status: 400 })
    }

    try {
      switch (event.type) {
        case 'checkout.session.completed': {
          const session = event.data.object as Stripe.Checkout.Session
          if (session.mode === 'setup') {
            await processTrialSetupSession(session, ctx.supabaseAdmin)
            break
          }

          const email = String(
            session.client_reference_id ||
            session.metadata?.user_email ||
            session.customer_details?.email ||
            '',
          ).trim().toLowerCase()
          const subscriptionId = stringId(session.subscription)
          await syncSubscriptionById(subscriptionId, ctx.supabaseAdmin, email)
          break
        }

        case 'customer.subscription.created':
        case 'customer.subscription.updated':
        case 'customer.subscription.deleted': {
          const subscription = event.data.object as Stripe.Subscription
          await persistSubscription(subscription, ctx.supabaseAdmin)
          break
        }

        case 'invoice.paid':
        case 'invoice.payment_failed': {
          const invoice = event.data.object as Stripe.Invoice
          const subscriptionId = subscriptionIdFromInvoice(invoice)
          await syncSubscriptionById(subscriptionId, ctx.supabaseAdmin)
          break
        }

        default:
          console.log(`Stripe webhook: ignored event ${event.type}`)
      }

      return Response.json({ received: true, event: event.type })
    } catch (err) {
      console.error('Stripe webhook processing failed:', err)
      return new Response('Webhook processing failed', { status: 500 })
    }
  }),
}
