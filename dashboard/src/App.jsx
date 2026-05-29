import React, { useEffect, useMemo, useRef, useState } from "react";

const POLL_MS = 2000;

const DEFAULT_CFG = {
  mode: "DEMO",
  trade_amount: 5,
  trade_duration_sec: 15,
  confidence_threshold: 0.7,
  max_consecutive_losses: 5,
  max_daily_loss: 50,
  bypass_risk_in_demo: true,
  min_candles_required: 50,
  candle_interval_sec: 15,
};

function cn(...v) {
  return v.filter(Boolean).join(" ");
}

function classifyLog(line) {
  if (line.includes("New Candle")) return "text-sky-400";
  if (line.includes("Signal:")) return "text-yellow-300";
  if (line.includes("CLICKED")) return "text-fuchsia-400";
  if (line.includes("Trade Result: WIN")) return "text-emerald-400";
  if (line.includes("Trade Result: LOSS")) return "text-red-400";
  if (line.includes("Trade Result: DRAW")) return "text-yellow-300";
  if (line.toLowerCase().includes("error")) return "text-red-500";
  return "text-zinc-300";
}

function statusColor(status) {
  switch (status) {
    case "RUNNING":
      return "bg-emerald-500";
    case "TRADING":
      return "bg-yellow-400";
    case "WARMUP":
      return "bg-orange-400";
    default:
      return "bg-zinc-500";
  }
}

function confidenceLabel(v) {
  if (v >= 0.85) return "VERY HIGH";
  if (v >= 0.75) return "HIGH";
  if (v >= 0.65) return "MEDIUM";
  return "LOW";
}

function confidenceColor(v) {
  if (v >= 0.85) return "bg-emerald-500";
  if (v >= 0.75) return "bg-lime-500";
  if (v >= 0.65) return "bg-yellow-500";
  return "bg-red-500";
}

function Card({ children, className = "" }) {
  return (
    <div
      className={cn(
        "bg-zinc-900/80 backdrop-blur border border-zinc-800 rounded-2xl shadow-xl",
        className
      )}
    >
      {children}
    </div>
  );
}

function StatCard({ label, value, sub, valueClass = "" }) {
  return (
    <Card className="p-4">
      <div className="text-[11px] uppercase tracking-[0.2em] text-zinc-500">
        {label}
      </div>
      <div className={cn("text-3xl font-bold mt-2", valueClass)}>{value}</div>
      {sub && <div className="text-xs text-zinc-500 mt-2">{sub}</div>}
    </Card>
  );
}

function Row({ k, v, vClass = "" }) {
  return (
    <div className="flex items-center justify-between text-sm py-1">
      <span className="text-zinc-500">{k}</span>
      <span className={cn("font-semibold", vClass)}>{v}</span>
    </div>
  );
}

function NumField({ label, v, onChange, step = 1 }) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="text-zinc-500">{label}</span>
      <input
        type="number"
        step={step}
        value={v}
        onChange={(e) => onChange(Number(e.target.value))}
        className="bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2 text-sm outline-none focus:border-blue-500"
      />
    </label>
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
  const [showConfig, setShowConfig] = useState(false);

  const logRef = useRef(null);

  useEffect(() => {
    let alive = true;

    async function tick() {
      try {
        const [s, c, t, l] = await Promise.all([
          fetch("/status").then((r) => r.json()),
          fetch("/config").then((r) => r.json()),
          fetch("/logs").then((r) => r.json()),
          fetch("/bot-logs?lines=1000").then((r) => r.json()),
        ]);

        if (!alive) return;

        setStatus(s || {});

        const nextConfig = { ...DEFAULT_CFG, ...(c || {}) };
        setConfig(nextConfig);

        setForm((prev) => (prev === null ? nextConfig : prev));

        setTrades(Array.isArray(t) ? t : []);
        setLogs(l?.lines || []);
      } catch (_) {}
    }

    tick();
    const id = setInterval(tick, POLL_MS);

    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs]);

  function updateForm(patch) {
    setDirty(true);
    setForm((prev) => ({ ...(prev || DEFAULT_CFG), ...patch }));
  }

  async function save() {
    if (!form) return;

    setSaving(true);

    try {
      const r = await fetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });

      if (!r.ok) throw new Error(await r.text());

      const data = await r.json();
      const saved = { ...DEFAULT_CFG, ...(data.config || form) };

      setConfig(saved);
      setForm(saved);
      setDirty(false);
      setShowConfig(false);
    } catch (e) {
      alert("Save failed: " + e.message);
    } finally {
      setSaving(false);
    }
  }

  const pnl = Number(status.pnl || 0);
  const wins = Number(status.wins || 0);
  const draws = Number(status.draws || 0);

  const losses = Math.max(
    0,
    Number(status.trades || 0) - wins - draws
  );

  const confidence = Number(status.last_confidence || 0.5);

  const confidencePct = useMemo(() => {
    return Math.max(5, Math.min(100, confidence * 100));
  }, [confidence]);

  return (
    <div className="h-screen bg-black text-zinc-100 p-3 flex flex-col gap-3 overflow-hidden">

      {/* HEADER */}
      <div className="flex items-center gap-4">
        <div className="text-3xl font-black tracking-tight">
          TD Agent Bot
        </div>

        <div className="flex items-center gap-2">
          <div className={cn("w-3 h-3 rounded-full", statusColor(status.bot_status))} />
          <span className="text-zinc-400 text-sm font-medium">
            {status.bot_status || "OFFLINE"}
          </span>
        </div>

        <div
          className={cn(
            "px-3 py-1 rounded-lg text-xs font-bold",
            config.mode === "DEMO" ? "bg-blue-600" : "bg-red-600"
          )}
        >
          {config.mode}
        </div>

        <div className="text-zinc-400 text-lg">
          {status.symbol || "EURUSD_otc"}
        </div>

        <button
          onClick={() => setShowConfig(true)}
          className="ml-auto bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 rounded-xl px-4 py-2 text-sm"
        >
          Config
        </button>
      </div>

      {/* KPI GRID */}
      <div className="grid grid-cols-4 gap-3">
        <StatCard
          label="Balance"
          value={`$${Number(status.balance || 0).toFixed(2)}`}
        />

        <StatCard
          label="Session PnL"
          value={`${pnl >= 0 ? "+" : ""}$${pnl.toFixed(2)}`}
          valueClass={pnl >= 0 ? "text-emerald-400" : "text-red-400"}
        />

          <StatCard
          label="Win Rate"
          value={`${Number(status.win_rate || 0).toFixed(1)}%`}
          sub={`${wins} wins • ${losses} losses`}
          valueClass={
            Number(status.win_rate || 0) >= 55
              ? "text-emerald-400"
              : Number(status.win_rate || 0) >= 50
              ? "text-yellow-300"
              : "text-red-400"
          }
        />

        <StatCard
          label="Trades"
        value={
            draws > 0
              ? `${wins}W / ${losses}L / ${draws}D`
              : `${wins}W / ${losses}L`
          }
          sub={`${status.trades || 0} total trades`}
        />
      </div>
      <div className="grid grid-cols-5 gap-2">

        <Card className="px-4 py-3 flex items-center justify-between">
          <span className="text-zinc-500 text-sm">
            Consecutive Losses
          </span>

          <span className="font-bold text-yellow-300">
            {status.consecutive_losses || 0}
          </span>
        </Card>

        <Card className="px-4 py-3 flex items-center justify-between">
          <span className="text-zinc-500 text-sm">
            Trade Amount
          </span>

          <span className="font-bold">
            ${config.trade_amount}
          </span>
        </Card>

        <Card className="px-4 py-3 flex items-center justify-between">
          <span className="text-zinc-500 text-sm">
            Trade Duration
          </span>

          <span className="font-bold text-cyan-300">
            {config.trade_duration_label || `${config.trade_duration_sec}s`}
          </span>
        </Card>

        <Card className="px-4 py-3 flex items-center justify-between">
          <span className="text-zinc-500 text-sm">
            Signal Confidence
          </span>

          <span className="font-bold text-sky-300">
            {(confidence * 100).toFixed(0)}%
          </span>
        </Card>

        <Card className="px-4 py-3 flex items-center justify-between">
          <span className="text-zinc-500 text-sm">
            Dataset Rows
          </span>

          <span className="font-bold text-fuchsia-300">
            {status.dataset_rows || 0}
          </span>
        </Card>

      </div>

      {/* MAIN */}
      <div className="grid grid-cols-12 gap-2 flex-1 min-h-0 overflow-hidden">

        {/* LEFT */}
        <div className="col-span-3 flex flex-col gap-3 min-h-0">

          <Card className="p-4">
            <div className="text-xs uppercase tracking-[0.2em] text-zinc-500 mb-4">
              Risk
            </div>

            {/* <div className="flex items-center justify-between mb-2">
              <span className="text-zinc-400 text-sm">Current Signal</span>
              <span className="font-bold text-lg">
                {status.last_signal || "NONE"}
              </span>
            </div> */}

            {/* <div className="mt-5">
              <div className="flex items-center justify-between text-sm mb-2">
                <span className="text-zinc-400">Confidence</span>
                <span className="font-bold">
                  {confidenceLabel(confidence)}
                </span>
              </div>

            <div className="w-full h-4 bg-zinc-800 rounded-full overflow-hidden border border-zinc-700">
                  <div
                  className={cn(
                    "h-full rounded-full transition-all duration-700",
                    confidenceColor(confidence)
                  )}
                  style={{ width: `${confidencePct}%` }}
                />
              </div>

              <div className="text-right text-xs text-zinc-500 mt-1">
                {(confidence * 100).toFixed(0)}%
              </div>
            </div> */}

            <div className="mt-6 border-t border-zinc-800 pt-4">
              <Row k="Last Result" v={status.last_result || "—"}
                vClass={
                  status.last_result === "WIN"
                    ? "text-emerald-400"
                    : status.last_result === "LOSS"
                    ? "text-red-400"
                    : status.last_result === "DRAW"
                    ? "text-yellow-300"
                    : ""
                }
              />

              {/* <Row
                k="Consecutive Losses"
                v={`${status.consecutive_losses || 0} / ${config.max_consecutive_losses}`}
              /> */}

              <Row
                k="Daily Loss"
                v={`$${Number(status.daily_loss || 0).toFixed(2)} / $${config.max_daily_loss}`}
              />
            </div>
          </Card>

          <Card className="p-5 flex-1 min-h-0 overflow-hidden">
            <div className="flex items-center justify-between mb-3">
              <div className="text-xs uppercase tracking-[0.2em] text-zinc-500">
                Bot Logs
              </div>
            </div>

            <div
              ref={logRef}
              className="overflow-auto h-full max-h-[420px] text-[11px] font-mono bg-black rounded-xl p-2 border border-zinc-800"
            >
              {logs.map((line, i) => (
                <div key={i} className={classifyLog(line)}>
                  {line}
                </div>
              ))}
            </div>
          </Card>
        </div>

        {/* CENTER */}
        <div className="col-span-5 flex flex-col gap-3 min-h-0">

          <Card className="p-4 flex flex-col justify-center items-center">
              {/* <div className="text-xs uppercase tracking-[0.2em] text-zinc-500 mb-6">
              Trade Status
            </div> */}

            <div className="text-4xl font-black tracking-tight mb-3">
              Bot Status : {status.bot_status || "IDLE"}
            </div>

           <div
              className={cn(
                "px-5 py-2 rounded-full text-sm font-bold mb-5",
                status.bot_status === "RUNNING"
                  ? "bg-emerald-500/20 text-emerald-400"
                  : status.bot_status === "TRADING"
                  ? "bg-yellow-500/20 text-yellow-300"
                  : "bg-zinc-800 text-zinc-400"
              )}
            >
              {status.last_signal || "WAITING"}
            </div>

            <div className="grid grid-cols-2 gap-4 w-full max-w-md text-sm">

              <div className="bg-zinc-900 rounded-xl p-3 border border-zinc-800">
                <div className="text-zinc-500 text-xs mb-1">Confidence</div>
                <div className="font-bold text-cyan-300">
                  {(confidence * 100).toFixed(0)}%
                </div>
              </div>

              <div className="bg-zinc-900 rounded-xl p-3 border border-zinc-800">
                <div className="text-zinc-500 text-xs mb-1">Expiry</div>
                <div className="font-bold text-yellow-300">
                  {config.trade_duration_label || `${config.trade_duration_sec}s`}
                </div>
              </div>

              <div className="bg-zinc-900 rounded-xl p-3 border border-zinc-800">
                <div className="text-zinc-500 text-xs mb-1">Risk Status</div>
                <div className="font-bold text-emerald-400">
                  SAFE
                </div>
              </div>

              <div className="bg-zinc-900 rounded-xl p-3 border border-zinc-800">
                <div className="text-zinc-500 text-xs mb-1">Model</div>
                <div className={cn(
                  "font-bold",
                  status.model_loaded
                    ? "text-emerald-400"
                    : "text-red-400"
                )}>
                  {status.model_loaded ? "ACTIVE" : "OFFLINE"}
                </div>
              </div>

            </div>
          </Card>

          <Card className="p-4">
            <div className="text-xs uppercase tracking-[0.2em] text-zinc-500 mb-4">
              Signal Filters
            </div>

            <div className="space-y-3 text-sm">
             <Decision
                ok={confidence >= config.confidence_threshold}
                text="Confidence above threshold"
              />

              <Decision
                ok={status.last_signal === "BUY"}
                text="Bullish trend alignment"
              />

              <Decision
                ok={status.last_signal === "SELL"}
                text="Bearish trend alignment"
              />

              <Decision
                ok={(status.consecutive_losses || 0) < config.max_consecutive_losses}
                text="Risk limits healthy"
              />
            </div>
          </Card>
          <Card className="p-4">
            <div className="text-xs uppercase tracking-[0.2em] text-zinc-500 mb-4">
            Model
            </div>

            <div className="space-y-2 text-sm">

              <Row
                k="Model"
                v={status.model_loaded ? "ACTIVE" : "NOT LOADED"}
                vClass={
                  status.model_loaded
                    ? "text-emerald-400"
                    : "text-red-400"
                }
              />
             <Row
                k="Executed Trades"
                v={`${status.trades || 0}`}
              />
              <Row
                k="Signal Confidence"
                v={`${(confidence * 100).toFixed(0)}% / ${(config.confidence_threshold * 100).toFixed(0)}%`}
                vClass={
                  confidence >= config.confidence_threshold
                    ? "text-emerald-400"
                    : "text-yellow-300"
                }
              />
              <Row
                k="Dataset Size"
                v={`${status.dataset_rows || 0} rows`}
              />
            </div>
          </Card>
          
        </div>

        {/* RIGHT */}
        <div className="col-span-4 flex flex-col gap-3 min-h-0">

          <Card className="p-5 flex-1 min-h-0 overflow-hidden">
            <div className="flex items-center justify-between mb-4">
              <div className="text-xs uppercase tracking-[0.2em] text-zinc-500">
                Recent Trades
              </div>

              <div className="text-xs text-zinc-500">
                {trades.length} entries
              </div>
              <div>
                <a
                  href="/trades/export"
                  target="_blank"
                  className="
                    px-3 py-1 rounded-lg
                    bg-cyan-500/20
                    text-cyan-300
                    text-xs font-bold
                    hover:bg-cyan-500/30
                  "
                >
                  Export CSV
                </a>

              </div>
            </div>

            <div className="overflow-auto h-full max-h-\[420px\]">
              <table className="w-full text-xs">
                <thead className="text-zinc-500 border-b border-zinc-800 sticky top-0 bg-zinc-900">
                  <tr>
                    <th className="text-left py-2">Time</th>
                    <th className="text-left py-2">Dir</th>
                    <th className="text-left py-2">Conf</th>
                    <th className="text-left py-2">Expiry</th>
                    <th className="text-left py-2">Result</th>
                    <th className="text-right py-2">PnL</th>
                  </tr>
                </thead>

                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i} className="border-b border-zinc-800/50">
                      <td className="py-2 text-zinc-400">
                        {t.ts ? new Date(t.ts).toLocaleTimeString() : "—"}
                      </td>

                      <td>
                        <span
                          className={cn(
                            "px-2 py-1 rounded-lg text-xs font-bold",
                            t.direction === "BUY"
                              ? "bg-emerald-500/20 text-emerald-400"
                              : "bg-red-500/20 text-red-400"
                          )}
                        >
                          {t.direction}
                        </span>
                      </td>
                      <td className="text-cyan-300 font-semibold">
                        {Number(t.confidence || 0).toFixed(2)}
                      </td>

                      <td className="text-yellow-300 font-semibold">
                        {t.duration || 0}s
                      </td>

                      <td>
                        <span
                          className={cn(
                            "font-semibold",
                            t.result === "WIN"
                              ? "text-emerald-400"
                              : t.result === "DRAW"
                              ? "text-yellow-300"
                              : "text-red-400"
                          )}
                        >
                          {t.result}
                        </span>
                      </td>

                      <td
                        className={cn(
                          "text-right font-bold",
                          Number(t.pnl || 0) >= 0
                            ? "text-emerald-400"
                            : "text-red-400"
                        )}
                      >
                        {Number(t.pnl || 0) >= 0 ? "+" : ""}
                        {Number(t.pnl || 0).toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      </div>

      {/* CONFIG DRAWER */}
      {showConfig && form && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex justify-end">
      <div className="w-[380px] h-full bg-zinc-950 border-l border-zinc-800 p-5 overflow-auto">
            <div className="flex items-center justify-between mb-6">
              <div>
                <div className="text-xl font-bold">Bot Config</div>
                <div className="text-sm text-zinc-500 mt-1">
                  Trading engine configuration
                </div>
              </div>

              <button
                onClick={() => setShowConfig(false)}
                className="text-zinc-400 hover:text-white"
              >
                ✕
              </button>
            </div>

            <div className="space-y-4">

              <div className="flex gap-2">
                {["DEMO", "LIVE"].map((m) => (
                  <button
                    key={m}
                    onClick={() => updateForm({ mode: m })}
                    className={cn(
                      "flex-1 py-3 rounded-xl font-bold",
                      form.mode === m
                        ? m === "DEMO"
                          ? "bg-blue-600"
                          : "bg-red-600"
                        : "bg-zinc-800 text-zinc-400"
                    )}
                  >
                    {m}
                  </button>
                ))}
              </div>

              <label className="flex items-center justify-between text-sm bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-3">
                <span>Bypass Risk (DEMO)</span>
                <input
                  type="checkbox"
                  checked={form.bypass_risk_in_demo}
                  onChange={(e) => updateForm({ bypass_risk_in_demo: e.target.checked })}
                />
              </label>
              
              
              <NumField
                label="Trade Amount"
                v={form.trade_amount}
                onChange={(v) => updateForm({ trade_amount: v })}
              />

 <label className="flex flex-col gap-1 text-xs">
              <span className="text-zinc-500">Trade Duration Mode</span>

            <select className="bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2 text-sm"
              value={config.duration_mode || "MANUAL"}
              onChange={(e) =>
                setConfig({
                  ...config,
                  duration_mode: e.target.value,
                })
              }
            >
              <option value="MANUAL">MANUAL</option>
              <option value="AUTO">AUTO</option>
            </select>
</label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-zinc-500">Trade Expiry</span>
                <select disabled={config.duration_mode === "AUTO"}
                  value={form.trade_duration_sec}
                  onChange={(e) => updateForm({ trade_duration_sec: Number(e.target.value) })}
                  className="bg-zinc-800 border border-zinc-700 rounded-xl px-3 py-2 text-sm"
                >
                  <option value={3}>S3</option>
                  <option value={15}>S15</option>
                  <option value={30}>S30</option>
                  <option value={60}>M1</option>
                  <option value={180}>M3</option>
                  <option value={300}>M5</option>
                  <option value={1800}>M30</option>
                  <option value={3600}>H1</option>
                </select>
              </label>

              <NumField
                label="Confidence Threshold"
                v={form.confidence_threshold}
                step={0.01}
                onChange={(v) => updateForm({ confidence_threshold: v })}
              />

              <NumField
                label="Max Consecutive Losses"
                v={form.max_consecutive_losses}
                onChange={(v) => updateForm({ max_consecutive_losses: v })}
              />

              <NumField
                label="Max Daily Loss"
                v={form.max_daily_loss}
                onChange={(v) => updateForm({ max_daily_loss: v })}
              />

              <NumField
                label="Min Candles"
                v={form.min_candles_required}
                onChange={(v) => updateForm({ min_candles_required: v })}
              />

              <NumField
                label="Candle Interval"
                v={form.candle_interval_sec}
                onChange={(v) => updateForm({ candle_interval_sec: v })}
              />


              <button
                onClick={save}
                disabled={saving}
                className="w-full mt-4 bg-emerald-600 hover:bg-emerald-500 rounded-xl py-3 font-bold"
              >
                {saving ? "Saving..." : dirty ? "Save Config *" : "Save Config"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Decision({ ok, text }) {
  return (
    <div className="flex items-center gap-3">
      <div
        className={cn(
          "w-2.5 h-2.5 rounded-full",
          ok ? "bg-emerald-400" : "bg-zinc-600"
        )}
      />

      <span className={ok ? "text-zinc-200" : "text-zinc-500"}>
        {text}
      </span>
    </div>
  );
}