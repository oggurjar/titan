/**
 * pika_check.js — Connect to Pika as an offline/cracked client and classify
 * the account based on the server's kick or chat response.
 *
 * Results:
 *   "premium"  — Pika kicked with an "Unauthorized" / Microsoft-required message
 *   "cracked"  — Pika showed /login or /register (auth plugin prompt)
 *   "banned"   — Account has an active ban on Pika
 *   "unknown"  — Could not determine (timeout, connection error, unexpected kick)
 *
 * Rate-limit kicks and wrong-case kicks are retried automatically.
 *
 * Output: one JSON line to stdout, then the process exits.
 */

'use strict';

const mineflayer = require('mineflayer');

const originalUsername = process.argv[2];
if (!originalUsername) {
    process.stdout.write(JSON.stringify({ error: 'No username supplied' }) + '\n');
    process.exit(1);
}

const HOST             = 'mp.pika.host';
const PORT             = 25565;
const MC_VERSION       = '1.8.9';
const MAX_RETRIES      = 5;       // attempts before giving up
const BASE_RETRY_DELAY = 5000;    // ms base wait between retries (exponential)
const CONNECT_TIMEOUT  = 18000;   // ms hard timeout per attempt
const POST_SPAWN_WAIT  = 6000;    // ms to wait for auth chat after spawning

let attempt = 0;

// ── Output helpers ──────────────────────────────────────────────────────────

function out(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}

function strip(text) {
    return (text || '').replace(/[§&][0-9a-fk-or]/gi, '').trim();
}

/**
 * Parse a Minecraft kick message — handles both raw JSON chat components
 * and plain strings. Returns a clean stripped string.
 */
function parseKickText(raw) {
    try {
        const obj = JSON.parse(raw);
        let text = obj.text || obj.translate || '';
        if (obj.extra && Array.isArray(obj.extra)) {
            text += obj.extra
                .map(e => (typeof e === 'string' ? e : (e.text || '')))
                .join('');
        }
        // Some servers nest further
        if (!text && obj.with && Array.isArray(obj.with)) {
            text = obj.with
                .map(e => (typeof e === 'object' ? (e.text || '') : String(e)))
                .join(' ');
        }
        return strip(text) || strip(raw);
    } catch {
        return strip(raw);
    }
}

// ── Retry scheduler ─────────────────────────────────────────────────────────

function scheduleRetry(retryWith, lastReason) {
    if (attempt >= MAX_RETRIES) {
        out({
            username: originalUsername,
            result:   'unknown',
            reason:   `Max retries (${MAX_RETRIES}) reached. Last: ${lastReason}`,
        });
        setTimeout(() => process.exit(0), 100);
        return;
    }
    // Exponential backoff: 5s, 10s, 20s, 40s …
    const delay = BASE_RETRY_DELAY * Math.pow(2, attempt - 1);
    setTimeout(() => tryAttempt(retryWith), delay);
}

// ── Main connection attempt ─────────────────────────────────────────────────

function tryAttempt(username) {
    attempt++;
    let done       = false;
    let spawnTimer = null;
    let hardTimer  = null;

    function finish(result, reason, retryWith) {
        if (done) return;
        done = true;

        if (spawnTimer) clearTimeout(spawnTimer);
        if (hardTimer)  clearTimeout(hardTimer);

        // Safely end the bot — ignore double-end errors
        try { bot.end(); } catch {}

        if (retryWith !== undefined) {
            scheduleRetry(retryWith || username, reason);
            return;
        }

        out({ username: originalUsername, result, reason });
        setTimeout(() => process.exit(0), 150);
    }

    const bot = mineflayer.createBot({
        host:                 HOST,
        port:                 PORT,
        username,
        version:              MC_VERSION,
        auth:                 'offline',
        hideErrors:           true,
        checkTimeoutInterval: 14000,
    });

    // Hard timeout — if nothing happens within CONNECT_TIMEOUT, give up / retry
    hardTimer = setTimeout(() => {
        finish(null, 'Connection timed out', username);   // retry
    }, CONNECT_TIMEOUT);
    hardTimer.unref();

    // ── Kicked ──────────────────────────────────────────────────────────────
    bot.on('kicked', (raw) => {
        const text = parseKickText(raw);
        const t    = text.toLowerCase();

        // Rate limited → retry with exponential backoff
        if (
            t.includes('logging in too fast') ||
            t.includes('try again later') ||
            t.includes('too many connections') ||
            t.includes('throttl')
        ) {
            finish(null, text, username);
            return;
        }

        // Wrong-case username → retry with the correct casing
        // e.g. "You should join using username Steve, not steve."
        const caseMatch = text.match(/join using username ([A-Za-z0-9_]+)/i);
        if (caseMatch) {
            finish(null, text, caseMatch[1]);
            return;
        }

        // Server restart / maintenance → retry
        if (
            t.includes('server is restarting') ||
            t.includes('server closed') ||
            t.includes('server is starting')
        ) {
            finish(null, text, username);
            return;
        }

        // ── Premium (Microsoft account required) — highest priority ─────────
        if (
            t.includes('unauthorized') ||
            t.includes('you cannot join') ||
            t.includes('not logged into your minecraft account') ||
            t.includes('restarting your minecraft client') ||
            t.includes('microsoft') ||
            t.includes('invalid session') ||
            t.includes('please verify')
        ) {
            finish('premium', text);
            return;
        }

        // ── Banned ───────────────────────────────────────────────────────────
        if (
            t.includes('you are banned') ||
            t.includes('tempban') ||
            t.includes('permanently banned') ||
            t.includes('appeal at') ||
            t.includes('blacklist') ||
            t.includes('banned from')
        ) {
            finish('banned', text);
            return;
        }

        // ── Everything else → unknown ────────────────────────────────────────
        finish('unknown', text);
    });

    // ── Spawned — wait for auth plugin chat ─────────────────────────────────
    bot.on('spawn', () => {
        spawnTimer = setTimeout(() => {
            finish('unknown', 'Spawned but no /login message received');
        }, POST_SPAWN_WAIT);
    });

    // ── Chat message after spawn ─────────────────────────────────────────────
    bot.on('message', (jsonMsg) => {
        const text = strip(jsonMsg.toString());
        const t    = text.toLowerCase();

        // Cracked: auth plugin asking for password
        if (
            t.includes('/login') ||
            t.includes('/register') ||
            t.includes('please login') ||
            t.includes('please register') ||
            t.includes('password') ||
            t.includes('log in to continue')
        ) {
            finish('cracked', text);
            return;
        }
    });

    // ── Network errors → retry if transient ─────────────────────────────────
    bot.on('error', (err) => {
        const msg = err.message || String(err);
        if (
            msg.includes('ECONNRESET')   ||
            msg.includes('ETIMEDOUT')    ||
            msg.includes('ECONNREFUSED') ||
            msg.includes('EPIPE')        ||
            msg.includes('socket hang up')
        ) {
            // Transient network issue — retry
            finish(null, msg, username);
        } else {
            finish('unknown', `Error: ${msg}`);
        }
    });

    bot.on('end', () => {
        if (!done) finish('unknown', 'Connection ended with no response');
    });
}

// Kick off first attempt
tryAttempt(originalUsername);
