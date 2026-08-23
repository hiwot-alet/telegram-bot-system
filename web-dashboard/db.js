/**
 * db.js — PostgreSQL connection pool for the Express dashboard.
 *
 * Uses the 'pg' package against the same PostgreSQL database the Telegram
 * bot writes to (see ../db/migrations/001_init.sql and ../bot/database.py).
 *
 * Required environment variables:
 *   DATABASE_URL   postgresql://user:password@host:port/dbname
 *
 * Optional environment variables:
 *   DB_POOL_MAX          max pooled clients (default: 10)
 *   DB_IDLE_TIMEOUT_MS   how long an idle client sits before being closed (default: 30000)
 *   DB_CONN_TIMEOUT_MS   how long to wait for a new connection before erroring (default: 5000)
 *   DATABASE_SSL         set to "true" to enable SSL (e.g. for managed Postgres in production)
 */

'use strict';

const { Pool } = require('pg');

const DATABASE_URL = process.env.DATABASE_URL;

if (!DATABASE_URL) {
  throw new Error('DATABASE_URL environment variable is not set');
}

const pool = new Pool({
  connectionString: DATABASE_URL,
  max: parseInt(process.env.DB_POOL_MAX || '10', 10),
  idleTimeoutMillis: parseInt(process.env.DB_IDLE_TIMEOUT_MS || '30000', 10),
  connectionTimeoutMillis: parseInt(process.env.DB_CONN_TIMEOUT_MS || '5000', 10),
  ssl: process.env.DATABASE_SSL === 'true' ? { rejectUnauthorized: false } : false,
});

pool.on('error', (err) => {
  // Errors on idle clients in the pool (e.g. the DB restarted) — log, don't crash the process.
  console.error('Unexpected error on idle PostgreSQL client', err);
});

/**
 * Run a single query against the pool.
 * @param {string} text - SQL text (use $1, $2, ... placeholders)
 * @param {Array} params - query parameters
 * @returns {Promise<import('pg').QueryResult>}
 */
async function query(text, params) {
  const start = Date.now();
  const result = await pool.query(text, params);
  if (process.env.DB_LOG_QUERIES === 'true') {
    const durationMs = Date.now() - start;
    console.log('executed query', { text, durationMs, rows: result.rowCount });
  }
  return result;
}

/**
 * Borrow a dedicated client for a multi-statement transaction.
 * Caller is responsible for calling client.release() when done.
 *
 * Usage:
 *   const client = await getClient();
 *   try {
 *     await client.query('BEGIN');
 *     ...
 *     await client.query('COMMIT');
 *   } catch (err) {
 *     await client.query('ROLLBACK');
 *     throw err;
 *   } finally {
 *     client.release();
 *   }
 */
async function getClient() {
  return pool.connect();
}

/**
 * Open a dedicated LISTEN connection on 'verification_events_channel'
 * (populated by the notify_verification_event() trigger in
 * db/migrations/001_init.sql) and invoke onNotification for every payload.
 *
 * This client is held open for the lifetime of the process and is separate
 * from the pool, since a LISTEN session must stay on one connection.
 *
 * @param {(payload: object) => void} onNotification
 * @returns {Promise<import('pg').PoolClient>} the listening client (call .release() to stop)
 */
async function listenForVerificationEvents(onNotification) {
  const client = await pool.connect();

  client.on('notification', (msg) => {
    try {
      const payload = JSON.parse(msg.payload);
      onNotification(payload);
    } catch (err) {
      console.error('Failed to parse verification_events_channel payload', err, msg.payload);
    }
  });

  client.on('error', (err) => {
    console.error('Error on verification_events_channel LISTEN connection', err);
  });

  await client.query('LISTEN verification_events_channel');
  console.log('Listening on verification_events_channel for real-time updates');

  return client;
}

/** Close the pool gracefully (e.g. on SIGTERM). */
async function closePool() {
  await pool.end();
}

process.on('SIGTERM', async () => {
  await closePool();
});
process.on('SIGINT', async () => {
  await closePool();
});

module.exports = {
  pool,
  query,
  getClient,
  listenForVerificationEvents,
  closePool,
};
