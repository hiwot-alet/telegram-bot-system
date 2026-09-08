/**
 * server.js — Express analytics dashboard for the Telegram OTP & Verification System.
 *
 * Routes:
 *   GET /                   Renders dashboard.ejs with aggregate stats + last 50
 *                           verification_events (joined with promoter/campaign info).
 *   GET /api/reports/csv    Streams a CSV export of all promoters, one row each,
 *                           with masked phone numbers.
 *   GET /api/events/stream  Server-Sent Events. Backed by a single long-lived
 *                           Postgres LISTEN on 'verification_events_channel'
 *                           (db.js's listenForVerificationEvents, driven by the
 *                           notify_verification_event() trigger in
 *                           db/migrations/001_init.sql). Each new
 *                           verification_events row is enriched with promoter/
 *                           campaign context and broadcast to every open browser
 *                           session.
 *
 * Required environment variables:
 *   DATABASE_URL         see db.js / ../db/migrations/001_init.sql
 *   ADMIN_USERNAME        admin login username
 *   ADMIN_PASSWORD_HASH   bcrypt hash of the admin login password (never the
 *                         plaintext password) — generate one with:
 *                           node -e "console.log(require('bcryptjs').hashSync('yourpassword', 10))"
 *   SESSION_SECRET        long random string used to sign the session cookie
 *
 * Optional environment variables:
 *   PORT           HTTP port to listen on (default: 3000)
 *
 * Run with:
 *   npm install
 *   npm start
 */

'use strict';

require('dotenv').config();

const path = require('path');
const express = require('express');
const session = require('express-session');
const bcrypt = require('bcryptjs');
const { query, listenForVerificationEvents } = require('./db');

const PORT = parseInt(process.env.PORT || '3000', 10);
const ADMIN_USERNAME = process.env.ADMIN_USERNAME;
const ADMIN_PASSWORD_HASH = process.env.ADMIN_PASSWORD_HASH;
const SESSION_SECRET = process.env.SESSION_SECRET;

if (!ADMIN_USERNAME || !ADMIN_PASSWORD_HASH) {
  throw new Error('ADMIN_USERNAME and ADMIN_PASSWORD_HASH environment variables are required');
}
if (!SESSION_SECRET) {
  throw new Error('SESSION_SECRET environment variable is required');
}

const app = express();
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));
app.disable('x-powered-by');
app.set('trust proxy', 1); // sits behind AletCloud's ingress/TLS termination

app.use(express.urlencoded({ extended: false }));
app.use(
  session({
    secret: SESSION_SECRET,
    resave: false,
    saveUninitialized: false,
    cookie: {
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      maxAge: 12 * 60 * 60 * 1000, // 12h
    },
  })
);

// ----------------------------------------------------------------------------
// Admin authentication — a single shared login gates the whole dashboard
// (stats, logs, exports, live stream). Session-based; the password is never
// stored or compared in plaintext.
// ----------------------------------------------------------------------------

function requireAuth(req, res, next) {
  if (req.session && req.session.authenticated) return next();
  return res.redirect('/login');
}

app.get('/login', (req, res) => {
  if (req.session.authenticated) return res.redirect('/');
  res.render('login', { error: null });
});

app.post('/login', async (req, res) => {
  const { username, password } = req.body || {};
  const usernameMatches = typeof username === 'string' && username === ADMIN_USERNAME;
  // Always run bcrypt.compare, even on a username mismatch, so response
  // timing doesn't reveal whether a given username exists.
  const passwordMatches = await bcrypt.compare(password || '', ADMIN_PASSWORD_HASH);

  if (!usernameMatches || !passwordMatches) {
    return res.status(401).render('login', { error: 'Invalid username or password.' });
  }

  req.session.regenerate((err) => {
    if (err) {
      console.error('Session regenerate failed:', err);
      return res.status(500).render('login', { error: 'Something went wrong. Try again.' });
    }
    req.session.authenticated = true;
    req.session.username = username;
    res.redirect('/');
  });
});

app.post('/logout', (req, res) => {
  req.session.destroy(() => res.redirect('/login'));
});

app.use(requireAuth);

// ----------------------------------------------------------------------------
// Shared formatting helpers
//
// These are used both for the server-rendered initial table (GET /) and for
// enriching live Postgres NOTIFY payloads before broadcasting them over SSE,
// so the two code paths can never drift into showing different shapes of data.
// ----------------------------------------------------------------------------

/**
 * Mask a phone number for display, e.g. "+251912345678" -> "+2519****678".
 * Keeps the country code + first subscriber digit and the last 3 digits;
 * everything in between is replaced with a fixed run of asterisks so the
 * mask length doesn't itself leak the number's length.
 */
function maskPhone(phone) {
  if (!phone) return '';
  const hasPlus = phone.trim().startsWith('+');
  const digits = (hasPlus ? phone.trim().slice(1) : phone.trim()).replace(/\D/g, '');

  if (digits.length <= 6) {
    // Too short to mask meaningfully in the "prefix...suffix" shape — just
    // hide everything but the last 2 digits.
    const visible = digits.slice(-2);
    return `${hasPlus ? '+' : ''}${'*'.repeat(Math.max(digits.length - 2, 0))}${visible}`;
  }

  const prefix = digits.slice(0, 4);
  const suffix = digits.slice(-3);
  return `${hasPlus ? '+' : ''}${prefix}****${suffix}`;
}

// Maps a verification_events.event_type to a human label + a "tone" that
// drives status-pill styling (verified=emerald, failed=red, pending=amber,
// info=slate). Kept in one place so dashboard.ejs's server-rendered rows and
// the client-side EventSource handler always agree on labels/colors.
const EVENT_STATUS = {
  otp_verified: { label: 'VERIFIED', tone: 'verified' },
  otp_failed: { label: 'FAILED', tone: 'failed' },
  otp_expired: { label: 'EXPIRED', tone: 'failed' },
  otp_sent: { label: 'CODE SENT', tone: 'pending' },
  phone_captured: { label: 'PHONE CAPTURED', tone: 'pending' },
  started: { label: 'STARTED', tone: 'info' },
  // Customer-verification events (see db/migrations/002_add_customers.sql) —
  // a promoter verifying someone else's phone number, not their own.
  customer_otp_verified: { label: 'CUSTOMER VERIFIED', tone: 'verified' },
  customer_otp_failed: { label: 'CUSTOMER FAILED', tone: 'failed' },
  customer_otp_expired: { label: 'CUSTOMER EXPIRED', tone: 'failed' },
  customer_otp_sent: { label: 'CUSTOMER CODE SENT', tone: 'pending' },
  customer_phone_captured: { label: 'CUSTOMER PHONE CAPTURED', tone: 'pending' },
  customer_started: { label: 'CUSTOMER STARTED', tone: 'info' },
};

function describeEvent(eventType) {
  return EVENT_STATUS[eventType] || { label: String(eventType || 'UNKNOWN').toUpperCase(), tone: 'info' };
}

/**
 * Normalize a verification_events row (server-rendered, joined with
 * promoters/campaigns/customers) into the flat shape the EJS template and
 * the client-side JS both render. `row` may come from a SQL join (Date
 * objects) or from a parsed Postgres NOTIFY payload merged with a
 * promoter/customer lookup (createdAt may be an ISO string) — both are
 * handled identically downstream since JS's Date constructor accepts either.
 *
 * When row.customer_id is set, the "subject" being verified is the customer
 * (name/phone come from the customers columns) but promoterName still names
 * who ran it — the dashboard renders that as a separate "Verified by" column
 * so it's never ambiguous who's who.
 */
function formatLogRow(row) {
  const status = describeEvent(row.event_type);
  const isCustomerEvent = row.customer_id !== null && row.customer_id !== undefined;

  const promoterName =
    row.full_name || row.telegram_username || (row.telegram_user_id ? `Telegram #${row.telegram_user_id}` : 'Unknown promoter');

  const subjectName = isCustomerEvent ? row.customer_full_name || 'Unnamed customer' : promoterName;
  const subjectPhone = isCustomerEvent ? row.customer_phone_number : row.phone_number;

  return {
    id: row.id,
    createdAt: row.created_at,
    subjectType: isCustomerEvent ? 'customer' : 'promoter',
    subjectName,
    maskedPhone: maskPhone(subjectPhone),
    promoterName,
    campaignName: row.campaign_name || '—',
    eventType: row.event_type,
    statusLabel: status.label,
    statusTone: status.tone,
  };
}

function csvEscape(value) {
  const str = value === null || value === undefined ? '' : String(value);
  if (/[",\n\r]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function toCsvRow(values) {
  return values.map(csvEscape).join(',') + '\r\n';
}

// Ethiopia has one timezone, UTC+3, with no DST — registration/verification
// timestamps are stored in UTC but always displayed in East Africa Time so
// promoters/admins reviewing the dashboard see local wall-clock time.
const EAT_TIMEZONE = 'Africa/Addis_Ababa';

function formatEAT(dateLike) {
  if (!dateLike) return '';
  return new Date(dateLike)
    .toLocaleString('en-GB', {
      timeZone: EAT_TIMEZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
    .replace(',', '');
}

// YYYY-MM-DD for "today" in EAT, used in export filenames so the date in the
// filename matches the EAT timestamps inside the file (a plain UTC date can
// be a day ahead late in the EAT evening).
function todayEATDateStamp() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: EAT_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const lookup = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${lookup.year}-${lookup.month}-${lookup.day}`;
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

// ----------------------------------------------------------------------------
// GET / — aggregate stats + last 50 verification logs
// ----------------------------------------------------------------------------

async function fetchAggregateStats() {
  const { rows } = await query(`
    SELECT
      (SELECT COUNT(*)::int FROM otp_verifications) AS total_requests,
      (SELECT COUNT(*)::int FROM promoters WHERE status = 'verified') AS verified_promoters,
      (SELECT COUNT(*)::int FROM otp_verifications WHERE status IN ('failed', 'expired')) AS failed_otps,
      (SELECT COUNT(*)::int FROM customers WHERE status = 'verified') AS verified_customers
  `);
  return rows[0];
}

async function fetchRecentLogs(limit) {
  const { rows } = await query(
    `
    SELECT ve.id, ve.event_type, ve.created_at, ve.customer_id,
           p.telegram_user_id, p.telegram_username, p.full_name, p.phone_number,
           c.name AS campaign_name,
           cu.full_name AS customer_full_name, cu.phone_number AS customer_phone_number
    FROM verification_events ve
    JOIN promoters p ON p.id = ve.promoter_id
    LEFT JOIN campaigns c ON c.id = ve.campaign_id
    LEFT JOIN customers cu ON cu.id = ve.customer_id
    ORDER BY ve.created_at DESC, ve.id DESC
    LIMIT $1
    `,
    [limit]
  );
  return rows.map(formatLogRow);
}

/**
 * Per-promoter customer counts, for the "who registered how many customers"
 * leaderboard. Counted from customer_verifications (one row per OTP attempt
 * a promoter ran for a customer) rather than verification_events, since that's
 * the same table the "Verified By" attribution elsewhere already relies on.
 * customers_contacted counts distinct customers the promoter ever ran an OTP
 * attempt for; customers_verified counts the subset currently verified.
 */
async function fetchPromoterLeaderboard() {
  const { rows } = await query(`
    SELECT p.id, p.telegram_username, p.full_name, p.city, p.created_at,
           c.name AS campaign_name,
           COUNT(DISTINCT cv.customer_id)::int AS customers_contacted,
           COUNT(DISTINCT CASE WHEN cu.status = 'verified' THEN cv.customer_id END)::int AS customers_verified
    FROM promoters p
    LEFT JOIN campaigns c ON c.id = p.campaign_id
    LEFT JOIN customer_verifications cv ON cv.promoter_id = p.id
    LEFT JOIN customers cu ON cu.id = cv.customer_id
    GROUP BY p.id, c.name
    ORDER BY p.created_at DESC
  `);
  return rows;
}

app.get('/', async (req, res, next) => {
  try {
    const [stats, logs, promoterLeaderboard] = await Promise.all([
      fetchAggregateStats(),
      fetchRecentLogs(50),
      fetchPromoterLeaderboard(),
    ]);
    res.render('dashboard', { stats, logs, promoterLeaderboard });
  } catch (err) {
    next(err);
  }
});

// ----------------------------------------------------------------------------
// GET /api/reports/csv — streamed CSV export, masked phone numbers
//
// Streams in batches (keyset pagination on promoters.id) rather than loading
// the whole table into memory, so this scales to a large promoter base
// without adding a streaming-query dependency beyond plain 'pg'.
// ----------------------------------------------------------------------------

app.get('/api/reports/csv', async (req, res) => {
  const today = todayEATDateStamp();
  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="verification_report_${today}.csv"`);

  res.write(
    toCsvRow([
      'Telegram Username',
      'Full Name',
      'Phone (masked)',
      'Campaign',
      'Status',
      'Started At (EAT)',
      'Verified At (EAT)',
      'Time To Verify',
      'Customers Registered',
      'Customers Verified',
    ])
  );

  const BATCH_SIZE = 500;
  let lastId = 0;

  try {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { rows } = await query(
        `
        SELECT p.id, p.telegram_username, p.full_name, p.phone_number,
               c.name AS campaign_name, p.status, p.created_at, p.verified_at,
               COALESCE(cust.customers_contacted, 0) AS customers_contacted,
               COALESCE(cust.customers_verified, 0) AS customers_verified
        FROM promoters p
        LEFT JOIN campaigns c ON c.id = p.campaign_id
        LEFT JOIN (
          SELECT cv.promoter_id,
                 COUNT(DISTINCT cv.customer_id)::int AS customers_contacted,
                 COUNT(DISTINCT CASE WHEN cu.status = 'verified' THEN cv.customer_id END)::int AS customers_verified
          FROM customer_verifications cv
          JOIN customers cu ON cu.id = cv.customer_id
          GROUP BY cv.promoter_id
        ) cust ON cust.promoter_id = p.id
        WHERE p.id > $1
        ORDER BY p.id
        LIMIT $2
        `,
        [lastId, BATCH_SIZE]
      );

      if (rows.length === 0) break;

      for (const row of rows) {
        const timeToVerify =
          row.verified_at && row.created_at
            ? formatDuration(new Date(row.verified_at) - new Date(row.created_at))
            : '';
        res.write(
          toCsvRow([
            row.telegram_username || '',
            row.full_name || '',
            maskPhone(row.phone_number),
            row.campaign_name || '',
            row.status,
            formatEAT(row.created_at),
            formatEAT(row.verified_at),
            timeToVerify,
            row.customers_contacted,
            row.customers_verified,
          ])
        );
      }

      lastId = rows[rows.length - 1].id;
      if (rows.length < BATCH_SIZE) break;
    }
  } catch (err) {
    console.error('CSV export failed mid-stream:', err);
    // Headers (and likely some rows) are already flushed to the client, so
    // we can't fall back to a JSON error response at this point — just end
    // the stream and let the client see a truncated file.
  } finally {
    res.end();
  }
});

// ----------------------------------------------------------------------------
// GET /api/reports/customers/csv — streamed CSV export of verified customers,
// one row per customer, attributed to whichever promoter most recently ran
// their verification (a customer could in principle be re-verified by a
// different promoter later — this reports the latest attempt's promoter).
// ----------------------------------------------------------------------------

app.get('/api/reports/customers/csv', async (req, res) => {
  const today = todayEATDateStamp();
  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="customer_verification_report_${today}.csv"`);

  res.write(
    toCsvRow([
      'Customer Name',
      'Customer Phone (masked)',
      'Verified By (Promoter)',
      'Promoter Telegram Username',
      'Campaign',
      'Status',
      'First Contacted (EAT)',
      'Verified At (EAT)',
      'Time To Verify',
    ])
  );

  const BATCH_SIZE = 500;
  let lastId = 0;

  try {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { rows } = await query(
        `
        SELECT cu.id, cu.full_name, cu.phone_number, cu.status, cu.created_at, cu.verified_at,
               p.full_name AS promoter_full_name, p.telegram_username AS promoter_telegram_username,
               c.name AS campaign_name
        FROM customers cu
        LEFT JOIN LATERAL (
          SELECT promoter_id FROM customer_verifications
          WHERE customer_id = cu.id
          ORDER BY created_at DESC
          LIMIT 1
        ) latest_cv ON true
        LEFT JOIN promoters p ON p.id = latest_cv.promoter_id
        LEFT JOIN campaigns c ON c.id = p.campaign_id
        WHERE cu.id > $1
        ORDER BY cu.id
        LIMIT $2
        `,
        [lastId, BATCH_SIZE]
      );

      if (rows.length === 0) break;

      for (const row of rows) {
        const timeToVerify =
          row.verified_at && row.created_at
            ? formatDuration(new Date(row.verified_at) - new Date(row.created_at))
            : '';
        res.write(
          toCsvRow([
            row.full_name || '',
            maskPhone(row.phone_number),
            row.promoter_full_name || '',
            row.promoter_telegram_username || '',
            row.campaign_name || '',
            row.status,
            formatEAT(row.created_at),
            formatEAT(row.verified_at),
            timeToVerify,
          ])
        );
      }

      lastId = rows[rows.length - 1].id;
      if (rows.length < BATCH_SIZE) break;
    }
  } catch (err) {
    console.error('Customer CSV export failed mid-stream:', err);
  } finally {
    res.end();
  }
});

// ----------------------------------------------------------------------------
// GET /api/promoters/:id/customers/csv — the list of customers a single
// promoter has registered (i.e. run at least one OTP attempt for), with
// masked phone numbers. Linked from the promoter leaderboard on the
// dashboard so "how many" (the count) and "which ones, by name" (this
// export) are both one click away.
// ----------------------------------------------------------------------------

app.get('/api/promoters/:id/customers/csv', async (req, res) => {
  const promoterId = parseInt(req.params.id, 10);
  if (!Number.isInteger(promoterId)) {
    res.status(400).send('Invalid promoter id');
    return;
  }

  const { rows: promoterRows } = await query(
    'SELECT telegram_username, full_name FROM promoters WHERE id = $1',
    [promoterId]
  );
  if (promoterRows.length === 0) {
    res.status(404).send('Promoter not found');
    return;
  }
  const promoter = promoterRows[0];

  const today = todayEATDateStamp();
  const safeName = (promoter.telegram_username || promoter.full_name || `promoter-${promoterId}`).replace(
    /[^a-z0-9_-]/gi,
    '_'
  );
  res.setHeader('Content-Type', 'text/csv; charset=utf-8');
  res.setHeader('Content-Disposition', `attachment; filename="customers_by_${safeName}_${today}.csv"`);

  res.write(
    toCsvRow([
      'Customer Name',
      'Customer Phone (masked)',
      'Status',
      'First Contacted (EAT)',
      'Verified At (EAT)',
      'Time To Verify',
    ])
  );

  try {
    const { rows } = await query(
      `
      SELECT cu.id, cu.full_name, cu.phone_number, cu.status,
             MIN(cv.created_at) AS first_contacted_at,
             MAX(cu.verified_at) AS verified_at
      FROM customer_verifications cv
      JOIN customers cu ON cu.id = cv.customer_id
      WHERE cv.promoter_id = $1
      GROUP BY cu.id
      ORDER BY first_contacted_at DESC
      `,
      [promoterId]
    );

    for (const row of rows) {
      const timeToVerify =
        row.verified_at && row.first_contacted_at
          ? formatDuration(new Date(row.verified_at) - new Date(row.first_contacted_at))
          : '';
      res.write(
        toCsvRow([
          row.full_name || '',
          maskPhone(row.phone_number),
          row.status,
          formatEAT(row.first_contacted_at),
          formatEAT(row.verified_at),
          timeToVerify,
        ])
      );
    }
  } catch (err) {
    console.error('Promoter customer CSV export failed mid-stream:', err);
  } finally {
    res.end();
  }
});

// ----------------------------------------------------------------------------
// GET /api/events/stream — SSE, backed by Postgres LISTEN/NOTIFY
// ----------------------------------------------------------------------------

const sseClients = new Set();
const HEARTBEAT_INTERVAL_MS = 30000;

async function enrichVerificationEvent(payload) {
  // customer_id is only present on customer_* events; the LEFT JOIN against
  // a parameter (rather than a real FK column) is intentional — when
  // payload.customer_id is null it simply matches no row, leaving all
  // customer_* columns null, which is exactly the promoter-only-event case.
  const { rows } = await query(
    `
    SELECT p.telegram_user_id, p.telegram_username, p.full_name, p.phone_number,
           c.name AS campaign_name,
           cu.full_name AS customer_full_name, cu.phone_number AS customer_phone_number
    FROM promoters p
    LEFT JOIN campaigns c ON c.id = p.campaign_id
    LEFT JOIN customers cu ON cu.id = $2
    WHERE p.id = $1
    `,
    [payload.promoter_id, payload.customer_id || null]
  );
  const promoter = rows[0] || {};

  return formatLogRow({
    id: payload.id,
    event_type: payload.event_type,
    created_at: payload.created_at,
    customer_id: payload.customer_id || null,
    telegram_user_id: promoter.telegram_user_id,
    telegram_username: promoter.telegram_username,
    full_name: promoter.full_name,
    phone_number: promoter.phone_number,
    campaign_name: promoter.campaign_name,
    customer_full_name: promoter.customer_full_name,
    customer_phone_number: promoter.customer_phone_number,
  });
}

function broadcast(eventName, data) {
  const payload = `event: ${eventName}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const clientRes of sseClients) {
    clientRes.write(payload);
  }
}

let listenerStarted = false;

async function startVerificationEventListener() {
  if (listenerStarted) return;
  listenerStarted = true;

  await listenForVerificationEvents(async (payload) => {
    try {
      const enriched = await enrichVerificationEvent(payload);
      broadcast('verification_event', enriched);
    } catch (err) {
      console.error('Failed to enrich/broadcast verification event:', err);
    }
  });
}

app.get('/api/events/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache, no-transform');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no'); // avoid buffering if sitting behind nginx
  res.flushHeaders();

  res.write('retry: 5000\n\n');
  sseClients.add(res);

  const heartbeat = setInterval(() => {
    res.write(': heartbeat\n\n');
  }, HEARTBEAT_INTERVAL_MS);

  req.on('close', () => {
    clearInterval(heartbeat);
    sseClients.delete(res);
  });
});

// ----------------------------------------------------------------------------
// Error handling & startup
// ----------------------------------------------------------------------------

app.use((req, res) => {
  res.status(404).send('Not found');
});

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).send('Something went wrong.');
});

async function main() {
  await startVerificationEventListener();
  app.listen(PORT, () => {
    console.log(`Dashboard listening on http://localhost:${PORT}`);
  });
}

if (require.main === module) {
  main().catch((err) => {
    console.error('Failed to start dashboard:', err);
    process.exit(1);
  });
}

module.exports = {
  app,
  maskPhone,
  describeEvent,
  formatLogRow,
  csvEscape,
  toCsvRow,
  formatDuration,
  formatEAT,
};
