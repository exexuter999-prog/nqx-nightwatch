/**
 * NightwatchState — trading account 単位の Durable Object。
 *
 * ここが activeScenario と position の唯一の正本。
 *   - SQLite storage に state doc / event log / nonce を持つ。
 *   - state 更新と event 記録は transactionSync で同一の整合性境界に入れる。
 *   - Hibernatable WebSocket で差分を push する(接続中もアイドル時は課金対象外)。
 *   - シナリオ期限は alarm とサーバー時刻の両方で消す。Bot が落ちていても消える。
 *
 * この DO は発注しない。発注経路を一切持たない。
 */
import { applyEvent, buildManualHalt, emptyState, projectState, sweepExpired, validateAccountPrefs } from "./state_machine.js";

const NONCE_RETENTION_MS = 24 * 60 * 60 * 1000;
//: 掃除の間隔。保持窓(24h)より十分短ければ滞留は増えない。
const PRUNE_INTERVAL_MS = 10 * 60 * 1000;
const EVENT_RETENTION = 500;
//: チャットの保持件数と 1 通の上限。無制限に伸ばさない。
const CHAT_RETENTION = 300;
const CHAT_MAX_CHARS = 4000;
const AUTOTRADE_ARM_SCHEMA = "NQX_AUTOTRADE_ARM/1";
const AUTOTRADE_DEFAULT_MINUTES = 420;
const AUTOTRADE_HARD_MAX_MINUTES = 720;

export class NightwatchState {
  #lastPruneMs = 0;

  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.sql = ctx.storage.sql;
    this.ctx.blockConcurrencyWhile(async () => {
      this.#migrate();
      // ping/pong は hibernation を解除せずに処理させる。
      try {
        this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
      } catch {
        // 古い compatibility date では未対応。接続自体は動く。
      }
    });
  }

  #migrate() {
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS state_doc (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        doc TEXT NOT NULL,
        updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        stream TEXT NOT NULL,
        revision INTEGER NOT NULL,
        nonce TEXT NOT NULL,
        accepted INTEGER NOT NULL,
        reason TEXT,
        transitions TEXT
      );
      CREATE TABLE IF NOT EXISTS consumed_nonces (
        nonce TEXT PRIMARY KEY,
        at INTEGER NOT NULL
      );
      /* PRIMARY KEY は nonce なので、#prune の "WHERE at < ?" は索引が無いと
         毎回この表を全走査する。24 時間分(数千行)を publish のたびに読むため、
         2026-09-09 に Durable Objects の rows read 上限を割り、Worker が全経路で
         1101 を返した。索引を張ると走査は実際に消す行だけになる。 */
      CREATE INDEX IF NOT EXISTS idx_consumed_nonces_at ON consumed_nonces (at);
      CREATE TABLE IF NOT EXISTS order_intents (
        idempotency_key TEXT PRIMARY KEY,
        at INTEGER NOT NULL,
        detail TEXT
      );
      /* R38: Mini App と Claude のやり取り。**発注経路とは完全に分離**する。
         ここに入る値が state_doc へ流れる経路は無く、シナリオにも claim にも
         触れない。role は "user"(アプリから) か "claude"(PC から署名付き)。 */
      CREATE TABLE IF NOT EXISTS chat (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        reply_to INTEGER
      );
    `);
  }

  // -------------------------------------------------------------- state I/O

  #loadState(accountId, symbol) {
    const rows = this.sql.exec("SELECT doc FROM state_doc WHERE id = 1").toArray();
    if (!rows.length) return emptyState(accountId, symbol);
    try {
      const parsed = JSON.parse(rows[0].doc);
      // account/symbol は作成時に固定する。後から別物を書き込ませない。
      return parsed && typeof parsed === "object" ? parsed : emptyState(accountId, symbol);
    } catch {
      return emptyState(accountId, symbol);
    }
  }

  #saveState(state, nowMs) {
    this.sql.exec(
      "INSERT INTO state_doc (id, doc, updated_at) VALUES (1, ?, ?) " +
      "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
      JSON.stringify(state), nowMs,
    );
  }

  #recordEvent(nowMs, stream, revision, nonce, accepted, reason, transitions) {
    this.sql.exec(
      "INSERT INTO events (at, stream, revision, nonce, accepted, reason, transitions) VALUES (?, ?, ?, ?, ?, ?, ?)",
      nowMs, stream, revision, nonce, accepted ? 1 : 0, reason, JSON.stringify(transitions || []),
    );
  }

  #prune(nowMs) {
    // 掃除は保持窓に比べて十分に細かければよい。毎 publish で走らせる必要は無く、
    // 走らせるほど rows read を無駄に使う(上と同じ 1101 の一因)。
    if (nowMs - this.#lastPruneMs < PRUNE_INTERVAL_MS) return;
    this.#lastPruneMs = nowMs;
    this.sql.exec("DELETE FROM consumed_nonces WHERE at < ?", nowMs - NONCE_RETENTION_MS);
    this.sql.exec(
      "DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?",
      EVENT_RETENTION,
    );
  }

  // -------------------------------------------------------------- 期限 alarm

  #scheduleExpiry(state) {
    const deadlines = [];
    const scenarioExpiry = Date.parse(state.scenario?.expiresAt);
    if (Number.isFinite(scenarioExpiry)) deadlines.push(scenarioExpiry);
    const armExpiry = state.autotradeArm?.enabled === true
      ? Date.parse(state.autotradeArm.expiresAt) : NaN;
    if (Number.isFinite(armExpiry)) deadlines.push(armExpiry);
    if (deadlines.length) {
      // 1 秒余裕を持たせる。境界で起きて「まだ有効」と判定されるのを避ける。
      this.ctx.storage.setAlarm(Math.min(...deadlines) + 1000);
    }
  }

  async alarm() {
    const nowMs = Date.now();
    const state = this.#loadState(this.env.NQX_ACCOUNT_ID || "default", this.env.NQX_SYMBOL || "MNQU6");
    const swept = sweepExpired(state, nowMs);
    if (!swept.transitions.length) return;
    this.ctx.storage.transactionSync(() => {
      this.#saveState(swept.state, nowMs);
      this.#recordEvent(nowMs, "expiry", swept.state.seq, "server-expiry", 1, "expired by server clock", swept.transitions);
    });
    this.#scheduleExpiry(swept.state);
    this.#broadcast(projectState(swept.state, nowMs), swept.transitions);
  }

  // -------------------------------------------------------------- WebSocket

  #broadcast(view, transitions) {
    const message = JSON.stringify({ type: "delta", view, transitions: transitions || [] });
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(message);
      } catch {
        // 切れているソケットは close ハンドラで片付く。ここでは黙って飛ばす。
      }
    }
  }

  async webSocketMessage(ws, raw) {
    let parsed = null;
    try {
      parsed = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return;
    }
    // クライアントからの唯一の要求は「完全 snapshot をくれ」だけ。
    // 状態を書き換える経路はブラウザ側に無い。
    if (parsed && parsed.type === "resync") {
      const nowMs = Date.now();
      const state = this.#loadState(this.env.NQX_ACCOUNT_ID || "default", this.env.NQX_SYMBOL || "MNQU6");
      ws.send(JSON.stringify({ type: "snapshot", view: projectState(state, nowMs) }));
    }
  }

  async webSocketClose(ws, code, reason, wasClean) {
    try { ws.close(code, reason); } catch { /* already closed */ }
  }

  async webSocketError(ws) {
    try { ws.close(1011, "socket error"); } catch { /* already closed */ }
  }

  // -------------------------------------------------------------- HTTP

  async fetch(request) {
    const url = new URL(request.url);
    const nowMs = Date.now();
    const accountId = this.env.NQX_ACCOUNT_ID || "default";
    const symbol = this.env.NQX_SYMBOL || "MNQU6";

    if (url.pathname === "/ws") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket", { status: 426 });
      }
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      // acceptWebSocket = hibernation 対応。DO が眠っても接続は維持される。
      this.ctx.acceptWebSocket(server);
      const state = this.#loadState(accountId, symbol);
      server.send(JSON.stringify({ type: "snapshot", view: projectState(state, nowMs) }));
      return new Response(null, { status: 101, webSocket: client });
    }

    if (url.pathname === "/state" && request.method === "GET") {
      const state = this.#loadState(accountId, symbol);
      const swept = sweepExpired(state, nowMs);
      if (swept.transitions.length) {
        this.ctx.storage.transactionSync(() => {
          this.#saveState(swept.state, nowMs);
          this.#recordEvent(nowMs, "scenario", swept.state.seq, "server-expiry", 1, "expired by server clock", swept.transitions);
        });
        this.#broadcast(projectState(swept.state, nowMs), swept.transitions);
      }
      return this.#json({ ok: true, view: projectState(swept.state, nowMs) });
    }

    if (url.pathname === "/publish" && request.method === "POST") {
      return this.#publish(request, nowMs, accountId, symbol);
    }

    if (url.pathname === "/autotrade" && request.method === "POST") {
      return this.#setAutotrade(request, nowMs, accountId, symbol);
    }

    if (url.pathname === "/accountPrefs" && request.method === "POST") {
      return this.#setAccountPrefs(request, nowMs, accountId, symbol);
    }

    if (url.pathname === "/manualHalt" && request.method === "POST") {
      return this.#setManualHalt(request, nowMs, accountId, symbol);
    }

    // ---------------------------------------------------------------- chat
    // 発注経路には一切触れない。state_doc も claim も読み書きしない。
    if (url.pathname === "/chat" && request.method === "GET") {
      const after = Number(url.searchParams.get("after") || 0) || 0;
      const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit") || 100)));
      const rows = this.sql.exec(
        "SELECT seq, at, role, text, reply_to FROM chat WHERE seq > ? ORDER BY seq ASC LIMIT ?",
        after, limit,
      ).toArray();
      return this.#json({ ok: true, messages: rows });
    }

    if (url.pathname === "/chat" && request.method === "POST") {
      let body = null;
      try {
        body = await request.json();
      } catch {
        return this.#json({ ok: false, reason: "chat body is not json" }, 400);
      }
      const role = String(body?.role || "").trim().toLowerCase();
      const text = String(body?.text ?? "").trim();
      if (role !== "user" && role !== "claude") {
        return this.#json({ ok: false, reason: "chat role must be user or claude" }, 400);
      }
      if (!text || text.length > CHAT_MAX_CHARS) {
        return this.#json({ ok: false, reason: "chat text is empty or too long" }, 400);
      }
      const replyTo = Number(body?.replyTo);
      this.sql.exec(
        "INSERT INTO chat (at, role, text, reply_to) VALUES (?, ?, ?, ?)",
        nowMs, role, text, Number.isFinite(replyTo) && replyTo > 0 ? replyTo : null,
      );
      // 保持は直近 CHAT_RETENTION 件。無制限に伸ばさない。
      this.sql.exec(
        "DELETE FROM chat WHERE seq <= (SELECT MAX(seq) FROM chat) - ?", CHAT_RETENTION,
      );
      const row = this.sql.exec(
        "SELECT seq, at, role, text, reply_to FROM chat ORDER BY seq DESC LIMIT 1",
      ).toArray()[0];
      return this.#json({ ok: true, message: row });
    }

    if (url.pathname === "/events" && request.method === "GET") {
      const rows = this.sql.exec(
        "SELECT seq, at, stream, revision, accepted, reason, transitions FROM events ORDER BY seq DESC LIMIT 100",
      ).toArray();
      return this.#json({ ok: true, events: rows });
    }

    return new Response("not found", { status: 404 });
  }

  async #publish(request, nowMs, accountId, symbol) {
    let event = null;
    try {
      event = await request.json();
    } catch {
      return this.#json({ ok: false, reason: "body is not JSON" }, 400);
    }

    const nonce = String(event?.nonce || "");
    if (!nonce) return this.#json({ ok: false, reason: "nonce is required" }, 400);

    let result = null;
    let duplicate = false;

    // state 更新と event 記録と nonce 消費を一つのトランザクションに入れる。
    // ここが崩れると「送信済みなのに未記録」が起きるので分割しない。
    this.ctx.storage.transactionSync(() => {
      const existing = this.sql.exec("SELECT nonce FROM consumed_nonces WHERE nonce = ?", nonce).toArray();
      if (existing.length) {
        duplicate = true;
        return;
      }
      this.sql.exec("INSERT INTO consumed_nonces (nonce, at) VALUES (?, ?)", nonce, nowMs);

      const state = this.#loadState(accountId, symbol);
      result = applyEvent(state, event, nowMs);
      if (result.accepted) this.#saveState(result.state, nowMs);
      this.#recordEvent(nowMs, String(event.stream || "?"), Number(event.revision) || 0,
        nonce, result.accepted, result.reason, result.transitions);
      this.#prune(nowMs);
    });

    if (duplicate) {
      return this.#json({ ok: false, reason: "nonce has already been consumed", duplicate: true }, 409);
    }
    if (!result.accepted) {
      return this.#json({ ok: false, reason: result.reason }, 409);
    }

    this.#scheduleExpiry(result.state);
    const view = projectState(result.state, nowMs);
    this.#broadcast(view, result.transitions);
    // R27: accepted:true でも result.reason が付くことがある。tombstoneCycle()
    // は scenario を null にしながら accepted:true を返すため、reason を落とすと
    // 発行側からは「成功」にしか見えず、シナリオが消えたことに気付けない。
    // reason が入るのは 409 経路だけ、という非対称をここで解消する。
    return this.#json({
      ok: true, seq: result.state.seq, transitions: result.transitions, view,
      ...(result.reason ? { reason: result.reason } : {}),
    });
  }

  async #setAutotrade(request, nowMs, accountId, symbol) {
    let body = null;
    try {
      body = await request.json();
    } catch {
      return this.#json({ ok: false, reason: "autotrade body is not JSON" }, 400);
    }
    if (typeof body?.enabled !== "boolean") {
      return this.#json({ ok: false, reason: "autotrade.enabled must be boolean" }, 400);
    }

    const accountScope = [...new Set(String(this.env.NQX_AUTOTRADE_ACCOUNTS || "")
      .split(/[;,\n]+/).map((value) => value.trim()).filter(Boolean))].sort();
    if (body.enabled && accountScope.length === 0) {
      return this.#json({ ok: false, reason: "NQX_AUTOTRADE_ACCOUNTS is not configured" }, 500);
    }
    const configuredMax = Number(this.env.NQX_AUTOTRADE_MAX_MINUTES);
    const maxMinutes = Number.isFinite(configuredMax)
      ? Math.max(1, Math.min(AUTOTRADE_HARD_MAX_MINUTES, Math.floor(configuredMax)))
      : AUTOTRADE_HARD_MAX_MINUTES;
    const configuredDefault = Number(this.env.NQX_AUTOTRADE_MINUTES);
    const defaultMinutes = Number.isFinite(configuredDefault)
      ? Math.max(1, Math.min(maxMinutes, Math.floor(configuredDefault)))
      : Math.min(AUTOTRADE_DEFAULT_MINUTES, maxMinutes);
    const requestedMinutes = Number(body.ttlMinutes);
    const minutes = Number.isFinite(requestedMinutes)
      ? Math.max(1, Math.min(maxMinutes, Math.floor(requestedMinutes)))
      : defaultMinutes;
    const actor = String(request.headers.get("X-NQX-Authorized-User") || "").slice(0, 64);
    const authSource = String(request.headers.get("X-NQX-Auth-Source") || "").slice(0, 32);

    let next = null;
    let arm = null;
    let transitions = [];
    this.ctx.storage.transactionSync(() => {
      const loaded = sweepExpired(this.#loadState(accountId, symbol), nowMs).state;
      const revision = Number(loaded.revisions?.autotrade || 0) + 1;
      const previous = loaded.autotradeArm;
      if (body.enabled) {
        arm = {
          schemaVersion: AUTOTRADE_ARM_SCHEMA,
          armId: `app_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`,
          enabled: true,
          autotrade: true,
          live: true,
          kill: false,
          status: "LIVE",
          source: "TELEGRAM_MINI_APP",
          armedAt: new Date(nowMs).toISOString(),
          expiresAt: new Date(nowMs + minutes * 60_000).toISOString(),
          accountScope,
          symbol,
          actor,
          authSource,
        };
        transitions = [{ kind: "autotrade", from: previous?.enabled ? "LIVE" : "OFF",
          to: "LIVE", armId: arm.armId }];
      } else {
        arm = {
          schemaVersion: AUTOTRADE_ARM_SCHEMA,
          armId: previous?.armId || null,
          enabled: false,
          autotrade: false,
          live: false,
          kill: false,
          status: "OFF",
          source: "TELEGRAM_MINI_APP",
          armedAt: previous?.armedAt || null,
          expiresAt: previous?.expiresAt || null,
          disabledAt: new Date(nowMs).toISOString(),
          accountScope: accountScope.length ? accountScope : (previous?.accountScope || []),
          symbol: symbol || previous?.symbol || null,
          actor,
          authSource,
        };
        transitions = [{ kind: "autotrade", from: previous?.enabled ? "LIVE" : "OFF",
          to: "OFF", armId: arm.armId }];
      }
      next = {
        ...loaded,
        seq: loaded.seq + 1,
        revisions: { ...loaded.revisions, autotrade: revision },
        autotradeArm: arm,
      };
      this.#saveState(next, nowMs);
      this.#recordEvent(nowMs, "autotrade", revision, `app-${crypto.randomUUID()}`,
        1, body.enabled ? "AUTO enabled by authorized Mini App" : "AUTO disabled by authorized Mini App",
        transitions);
      this.#prune(nowMs);
    });

    this.#scheduleExpiry(next);
    const view = projectState(next, nowMs);
    this.#broadcast(view, transitions);
    return this.#json({ ok: true, arm: view.autotradeArm, view });
  }

  /**
   * 口座別ユーザー設定(ULTRA 対象・利益目標・DD上限)。
   *
   * 認証済み Mini App の全置換書き込み。注文は送らない — ここに入るのは
   * 「どの口座で ULTRA を使いたいか」という意図だけで、実際の枚数・リスクは
   * 監視PC側の ultra_mode + 実行契約エンベロープが毎回再計算・クランプする。
   */
  async #setAccountPrefs(request, nowMs, accountId, symbol) {
    let body = null;
    try {
      body = await request.json();
    } catch {
      return this.#json({ ok: false, reason: "accountPrefs body is not JSON" }, 400);
    }
    const checked = validateAccountPrefs(body?.prefs);
    if (!checked.ok) return this.#json({ ok: false, reason: checked.reason }, 400);

    const actor = String(request.headers.get("X-NQX-Authorized-User") || "").slice(0, 64);
    let next = null;
    let transitions = [];
    this.ctx.storage.transactionSync(() => {
      const loaded = sweepExpired(this.#loadState(accountId, symbol), nowMs).state;
      const revision = Number(loaded.revisions?.account_prefs || 0) + 1;
      const ultraIds = Object.keys(checked.map).filter((id) => checked.map[id].ultra);
      transitions = [{ kind: "account_prefs",
        from: Object.keys(loaded.accountPrefs?.map || {}).filter((id) => loaded.accountPrefs.map[id]?.ultra).join(",") || null,
        to: ultraIds.join(",") || null }];
      next = {
        ...loaded,
        seq: loaded.seq + 1,
        revisions: { ...loaded.revisions, account_prefs: revision },
        accountPrefs: {
          updatedAt: new Date(nowMs).toISOString(),
          updatedBy: actor || null,
          map: checked.map,
        },
      };
      this.#saveState(next, nowMs);
      this.#recordEvent(nowMs, "account_prefs", revision, `app-${crypto.randomUUID()}`,
        1, "account prefs updated by authorized Mini App", transitions);
      this.#prune(nowMs);
    });

    const view = projectState(next, nowMs);
    this.#broadcast(view, transitions);
    return this.#json({ ok: true, prefs: view.accountPrefs, view });
  }

  /**
   * R87: 設定ページの手動HALT。注文は送らない —— 保存するのは「止めたい」という意図だけで、
   * 実際に止めるのは監視PC(autotrade_engine.manual_halt)。撤退(KILL / flatten)は塞がない。
   */
  async #setManualHalt(request, nowMs, accountId, symbol) {
    let body = null;
    try {
      body = await request.json();
    } catch {
      return this.#json({ ok: false, reason: "manualHalt body is not JSON" }, 400);
    }
    const actor = String(request.headers.get("X-NQX-Authorized-User") || "").slice(0, 64);
    const authSource = String(request.headers.get("X-NQX-Auth-Source") || "").slice(0, 32);
    let next = null;
    let transitions = [];
    let rejected = null;
    this.ctx.storage.transactionSync(() => {
      const loaded = sweepExpired(this.#loadState(accountId, symbol), nowMs).state;
      const built = buildManualHalt(loaded.manualHalt, body, nowMs, actor, authSource);
      if (!built.ok) {
        rejected = built.reason;
        return;
      }
      const revision = Number(loaded.revisions?.manual_halt || 0) + 1;
      transitions = built.transitions;
      next = {
        ...loaded,
        seq: loaded.seq + 1,
        revisions: { ...loaded.revisions, manual_halt: revision },
        manualHalt: built.halt,
      };
      this.#saveState(next, nowMs);
      this.#recordEvent(nowMs, "manual_halt", revision, `app-${crypto.randomUUID()}`, 1,
        built.halt.enabled ? "MANUAL HALT engaged by authorized Mini App"
          : "MANUAL HALT released by authorized Mini App", transitions);
      this.#prune(nowMs);
    });
    if (rejected) return this.#json({ ok: false, reason: rejected }, 400);

    const view = projectState(next, nowMs);
    this.#broadcast(view, transitions);
    return this.#json({ ok: true, manualHalt: view.manualHalt, view });
  }

  #json(body, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }
}
