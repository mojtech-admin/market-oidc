import Stripe from 'npm:stripe@^22'
import { withSupabase } from 'npm:@supabase/server@^1'

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') as string)
const cryptoProvider = Stripe.createSubtleCryptoProvider()


function getSubscriptionCurrentPeriodEnd(subscription: any): number | null {
  // Stripe API versions from Basil onward expose current_period_end on
  // subscription items instead of the top-level Subscription object.
  const itemPeriodEnd =
    subscription?.items?.data?.[0]?.current_period_end ?? null;

  if (itemPeriodEnd) return itemPeriodEnd;

  // Backward-compatible fallback for older Stripe API versions.
  return subscription?.current_period_end ?? null;
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

  const payload = {
    subscription_status: subscription.status,
    stripe_subscription_id: subscription.id,
    stripe_customer_id: stringId(subscription.customer),
    subscription_current_period_end: isoFromUnix(getSubscriptionCurrentPeriodEnd(subscription)),
    subscription_cancel_at_period_end: Boolean(subscription.cancel_at_period_end),
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
