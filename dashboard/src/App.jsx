import React, { useEffect, useRef, useState } from "react";

const POLL_MS = 2000;

const DEFAULT_CFG = {
  mode: "DEMO",
  trade_amount: 5,
  trade_duration_sec: 60,
  confidence_threshold: 0.65,
  max_consecutive_losses: 3,
  max_daily_loss: 50,
  bypass_risk_in_demo: true,
  min_candles_required: 50,
  candle_interval_sec: 5,
};

function classifyLog(line) {
  if (line.includes("TICK =>")) return "text-zinc-500";
  if (line.includes("New Candle")) return "text-blue-400";
  if (line.includes("Signal:")) return "text-yellow-300";
  if (line.includes("CLICKED")) return "text-purple-400";
  if (line.includes("Trade Result: WIN")) return "text-green-400";
  if (line.includes("Trade Result: LOSS")) return "text-red-400";
  if (line.includes("BALANCE UPDATE")) return "text-cyan-400";
  if (line.includes("Too many") || line.includes("BLOCKED")) return "text-red-500";
  if (line.toLowerCase().includes("error")) return "text-red-400";
  return "text-zinc-300";
}

function Stat({ label, value, valueClass = "" }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex flex-col">
      <span className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</span>
      <span className={`text-2xl font-bold mt-1 ${valueClass}`}>{value}</span>
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState({});
  const [config, setConfig] = useState(DEFAULT_CFG);
  const [form, setForm] = useState(null);
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const logRef = useRef(null);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const [s, c, t, l] = await Promise.all([
          fetch("/status").then((r) => r.json()),
          fetch("/config").then((r) => r.json()),
          fetch("/logs").then((r) => r.json()),
          fetch("/bot-logs?lines=300").then((r) => r.json()),
        ]);
        if (!alive) return;
        setStatus(s || {});
        const nextConfig = { ...DEFAULT_CFG, ...(c || {}) };
        setConfig(nextConfig);
        setForm((prev) => (prev === null ? nextConfig : prev));
        setTrades(Array.isArray(t) ? t : []);
        setLogs(l?.lines || []);
      } catch (e) {
        // ignore — bot/api may be restarting
      }
    }
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { alive = false; clearInterval(id); };
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  function updateForm(patch) {
    setDirty(true);
    setForm((prev) => ({ ...(prev || DEFAULT_CFG), ...patch }));
  }

  function cleanNumber(value, fallback) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  function normalizedForm() {
    const base = { ...DEFAULT_CFG, ...(form || {}) };
    return {
      ...base,
      trade_amount: cleanNumber(base.trade_amount, DEFAULT_CFG.trade_amount),
      trade_duration_sec: Math.round(cleanNumber(base.trade_duration_sec, DEFAULT_CFG.trade_duration_sec)),
      confidence_threshold: cleanNumber(base.confidence_threshold, DEFAULT_CFG.confidence_threshold),
      max_consecutive_losses: Math.round(cleanNumber(base.max_consecutive_losses, DEFAULT_CFG.max_consecutive_losses)),
      max_daily_loss: cleanNumber(base.max_daily_loss, DEFAULT_CFG.max_daily_loss),
      min_candles_required: Math.round(cleanNumber(base.min_candles_required, DEFAULT_CFG.min_candles_required)),
      candle_interval_sec: Math.round(cleanNumber(base.candle_interval_sec, DEFAULT_CFG.candle_interval_sec)),
    };
  }

  async function save() {
    if (!form) return;
    setSaving(true);
    try {
      const payload = normalizedForm();
      const r = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await r.text());
      const data = await r.json();
      const saved = { ...DEFAULT_CFG, ...(data.config || payload) };
      setConfig(saved);
      setForm(saved);
      setDirty(false);
    } catch (e) {
      alert("Save failed: " + e.message);
    } finally {
      setSaving(false);
    }
  }

  const running = status.bot_status && status.bot_status !== "IDLE";
  const isDemo = (status.mode || config.mode) === "DEMO";
  const pnl = Number(status.pnl || 0);
  const wins = Number(status.wins || 0);
  const losses = Math.max(0, Number(status.trades || 0) - wins);

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-zinc-950 text-zinc-100 p-3 gap-3">
      {/* Header */}
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-bold tracking-tight">TD Agent Bot</h1>
        <span className="relative flex h-3 w-3">
          {running && <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>}
          <span className={`relative inline-flex rounded-full h-3 w-3 ${running ? "bg-emerald-500" : "bg-zinc-600"}`}></span>
        </span>
        <span className="text-xs text-zinc-400">{status.bot_status || "OFFLINE"}</span>
        <span className={`px-2 py-0.5 text-xs font-bold rounded ${isDemo ? "bg-blue-600" : "bg-red-600"}`}>
          {isDemo ? "DEMO" : "LIVE"}
        </span>
        <span className="text-xs text-zinc-400">{status.symbol || "—"}</span>
        <div className="ml-auto text-xs text-zinc-500">
          session: {status.session_started_at ? new Date(status.session_started_at).toLocaleTimeString() : "—"}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3">
        <Stat label="Balance" value={`$${Number(status.balance || 0).toFixed(2)}`} />
        <Stat
          label="PnL"
          value={`${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`}
          valueClass={pnl >= 0 ? "text-green-400" : "text-red-400"}
        />
        <Stat label="Win Rate" value={`${Number(status.win_rate || 0).toFixed(1)}%`} />
        <Stat label="Trades" value={`${wins}W / ${losses}L`} />
      </div>

      {/* Main panels */}
      <div className="flex gap-3 flex-1 min-h-0">
        {/* Risk */}
        <div className="w-60 bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex flex-col gap-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Risk</div>
          <Row k="Last Signal" v={status.last_signal || "—"} />
          <Row k="Last Result" v={status.last_result || "—"} vClass={
            status.last_result === "WIN" ? "text-green-400" :
            status.last_result === "LOSS" ? "text-red-400" : ""
          } />
          <Row k="Consec. Losses" v={`${status.consecutive_losses ?? 0} / ${config.max_consecutive_losses}`} />
          <Row k="Daily Loss" v={`$${Number(status.daily_loss || 0).toFixed(2)} / $${config.max_daily_loss}`} />
          <Row k="Wins" v={wins} />
        </div>

        {/* Config */}
        <div className="w-72 bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex flex-col gap-2 overflow-auto">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Config</div>
          {form && (
            <>
              <div className="flex gap-1">
                {["DEMO", "LIVE"].map((m) => (
                  <button
                    key={m}
                    onClick={() => updateForm({ mode: m })}
                    className={`flex-1 py-1 text-xs rounded font-bold ${
                      form.mode === m
                        ? m === "DEMO" ? "bg-blue-600" : "bg-red-600"
                        : "bg-zinc-800 text-zinc-400"
                    }`}
                  >{m}</button>
                ))}
              </div>
              <label className="flex items-center justify-between text-xs">
                <span>Bypass Risk (DEMO)</span>
                <input
                  type="checkbox"
                  checked={form.bypass_risk_in_demo}
                  onChange={(e) => updateForm({ bypass_risk_in_demo: e.target.checked })}
                />
              </label>
              <NumField label="Trade Amount $" v={form.trade_amount} step={1}
                onChange={(v) => updateForm({ trade_amount: v })} />
              <NumField label="Duration (s)" v={form.trade_duration_sec} step={5}
                onChange={(v) => updateForm({ trade_duration_sec: v })} />
              <NumField label="Confidence Threshold" v={form.confidence_threshold} step={0.05}
                onChange={(v) => updateForm({ confidence_threshold: v })} />
              <NumField label="Max Consec. Losses" v={form.max_consecutive_losses} step={1}
                onChange={(v) => updateForm({ max_consecutive_losses: v })} />
              <NumField label="Max Daily Loss $" v={form.max_daily_loss} step={5}
                onChange={(v) => updateForm({ max_daily_loss: v })} />
              <NumField label="Min Candles" v={form.min_candles_required} step={5}
                onChange={(v) => updateForm({ min_candles_required: v })} />
              <NumField label="Candle Interval (s)" v={form.candle_interval_sec} step={1}
                onChange={(v) => updateForm({ candle_interval_sec: v })} />
              <button
                onClick={save}
                disabled={saving}
                className="mt-auto bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-sm font-bold py-2 rounded"
              >{saving ? "Saving…" : dirty ? "Save Config *" : "Save Config"}</button>
            </>
          )}
        </div>

        {/* Recent trades */}
        <div className="w-80 bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex flex-col gap-2 min-h-0">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">Recent Trades</div>
          <div className="flex-1 overflow-auto">
            <table className="w-full text-xs">
              <thead className="text-zinc-500">
                <tr><th className="text-left">Time</th><th className="text-left">Dir</th><th className="text-left">Res</th><th className="text-right">PnL</th></tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i} className="border-t border-zinc-800">
                    <td className="py-1">{t.ts ? new Date(t.ts).toLocaleTimeString() : "—"}</td>
                    <td>{t.direction}</td>
                    <td className={t.result === "WIN" ? "text-green-400" : "text-red-400"}>{t.result}</td>
                    <td className={`text-right ${t.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {t.pnl >= 0 ? "+" : ""}{Number(t.pnl).toFixed(2)}
                    </td>
                  </tr>
                ))}
                {trades.length === 0 && (
                  <tr><td colSpan={4} className="text-center py-4 text-zinc-600">No trades yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Logs */}
        <div className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg p-3 flex flex-col min-h-0 min-w-0">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">Bot Logs</div>
          <div ref={logRef} className="flex-1 overflow-auto font-mono text-[11px] leading-tight bg-black/40 rounded p-2">
            {logs.map((line, i) => (
              <div key={i} className={classifyLog(line)}>{line}</div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ k, v, vClass = "" }) {
  return (
    <div className="flex justify-between text-xs">
      <span className="text-zinc-500">{k}</span>
      <span className={`font-medium ${vClass}`}>{String(v)}</span>
    </div>
  );
}

function NumField({ label, v, step, onChange }) {
  return (
    <label className="flex flex-col text-[11px] gap-0.5">
      <span className="text-zinc-500">{label}</span>
      <input
        type="number"
        step={step}
        value={v}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm"
      />
    </label>
  );
}
