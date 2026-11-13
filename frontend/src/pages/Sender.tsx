import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type AutoSend,
  type AutoSendSettings,
  type SenderGroup,
  type SenderJob,
  type SenderOverview,
  type SenderPerformance,
} from "../api";
import { ErrorBox, Spinner } from "../components";
import { dateTimeLabel, money } from "../format";

const STATE_LABEL: Record<string, string> = {
  off: "desligado (SENDER_URL vazio)",
  unreachable: "robô fora do ar",
  starting: "iniciando…",
  qr: "aguardando a leitura do QR code",
  authenticated: "autenticado, carregando…",
  ready: "conectado",
  disconnected: "desconectado",
};

const JOB_STYLE: Record<SenderJob["status"], string> = {
  queued: "bg-amber-50 text-amber-800 ring-amber-200",
  sent: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  failed: "bg-rose-50 text-rose-800 ring-rose-200",
  cancelled: "bg-slate-100 text-slate-600 ring-slate-200",
};
const JOB_LABEL: Record<SenderJob["status"], string> = { queued: "na fila", sent: "enviada", failed: "falhou", cancelled: "cancelada" };

function GroupPicker({ selected, onSaved }: { selected: string[]; onSaved: () => void }) {
  const [groups, setGroups] = useState<SenderGroup[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set(selected));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .senderGroups()
      .then((list) => (setGroups(list), setChosen(new Set(list.filter((g) => g.selected).map((g) => g.id)))))
      .catch((exc: Error) => setError(exc.message));
  }, []);

  async function save() {
    if (!groups) return;
    setBusy(true);
    setError("");
    try {
      await api.setSenderGroups(groups.filter((g) => chosen.has(g.id)).map((g) => ({ id: g.id, name: g.name })));
      onSaved();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-2 rounded-lg bg-white p-4 ring-1 ring-slate-200">
      <h2 className="font-semibold">Grupos que recebem as ofertas</h2>
      <p className="text-xs text-slate-500">Só grupos em que as pessoas pediram para estar. Denúncia de spam é o que derruba o número.</p>
      {error && <ErrorBox message={error} />}
      {!groups && !error && <Spinner />}
      {groups && groups.length === 0 && <p className="text-sm text-slate-600">O número conectado não está em nenhum grupo.</p>}
      <ul className="max-h-72 space-y-1 overflow-auto">
        {groups?.map((g) => (
          <li key={g.id}>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={chosen.has(g.id)}
                onChange={(e) => {
                  const next = new Set(chosen);
                  if (e.target.checked) next.add(g.id);
                  else next.delete(g.id);
                  setChosen(next);
                }}
              />
              {g.name} <span className="text-xs text-slate-500">{g.participants} participantes</span>
            </label>
          </li>
        ))}
      </ul>
      {groups && groups.length > 0 && (
        <button
          type="button"
          onClick={save}
          disabled={busy}
          className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          Salvar grupos ({chosen.size})
        </button>
      )}
    </section>
  );
}

function AutoSendCard({ initial, onChanged }: { initial?: AutoSend; onChanged: () => void }) {
  // Enquanto o backend não responde, o interruptor fica claramente desligado e travado.
  const [settings, setSettings] = useState<AutoSendSettings | null>(null);
  const [score, setScore] = useState(String(initial?.min_score ?? 70));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .senderAuto()
      .then((s) => (setSettings(s), setScore(String(s.min_score)), setError("")))
      .catch(() => setError("Não consegui ler o envio automático: ele segue desligado."));
  }, []);

  const enabled = settings?.enabled ?? false;
  const parsed = Number(score);
  const scoreValid = score.trim() !== "" && Number.isInteger(parsed) && parsed >= 0 && parsed <= 100;

  async function save(nextEnabled: boolean, minScore: number | null) {
    setBusy(true);
    setError("");
    try {
      const saved = await api.setSenderAuto(nextEnabled, minScore);
      setSettings(saved);
      setScore(String(saved.min_score));
      onChanged();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar.");
    } finally {
      setBusy(false);
    }
  }

  function toggle() {
    if (!enabled) {
      if (!scoreValid) {
        setError("Informe uma pontuação mínima de 0 a 100.");
        return;
      }
      if (!window.confirm("Ligar o envio automático? O robô passa a mandar mensagens nos grupos sozinho, sem você aprovar cada oferta.")) return;
    }
    void save(!enabled, scoreValid ? parsed : null);
  }

  return (
    <section className="space-y-2 rounded-lg bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-semibold">Envio automático</h2>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="Envio automático"
          onClick={toggle}
          disabled={busy || !settings}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${enabled ? "bg-emerald-600" : "bg-slate-300"}`}
        >
          <span className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-all ${enabled ? "left-[22px]" : "left-0.5"}`} />
        </button>
        <span className={`text-sm font-medium ${enabled ? "text-emerald-700" : "text-slate-600"}`}>{enabled ? "Ligado" : "Desligado"}</span>
      </div>
      <p className="text-xs text-slate-500">
        Desligado por padrão. Ligado, o robô enfileira sozinho as ofertas prontas (Mercado Livre com link colado e Shopee) com pontuação igual ou acima do
        corte, respeitando o limite diário, o intervalo, a janela de horário e a conferência de preço. Dá para pausar a qualquer momento.
      </p>
      {error && <ErrorBox message={error} />}
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Pontuação mínima
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            value={score}
            onChange={(e) => setScore(e.target.value)}
            disabled={!settings}
            className="w-20 rounded-md border border-slate-300 bg-white px-2 py-1 disabled:opacity-50"
          />
        </label>
        <button
          type="button"
          onClick={() => void save(enabled, parsed)}
          disabled={busy || !settings || !scoreValid || parsed === settings.min_score}
          className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50 disabled:opacity-40"
        >
          Salvar corte
        </button>
        {settings && <span className="text-xs text-slate-500">padrão: {settings.default_min_score}</span>}
      </div>
    </section>
  );
}

function SalesByGroupCard() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<SenderPerformance | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    api
      .senderPerformance(days)
      .then((body) => !cancelled && setData(body))
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
  }, [days]);

  if (data && !data.configured) return null; // sem Shopee ligada não há o que mostrar

  return (
    <section className="space-y-2 rounded-lg bg-white p-4 ring-1 ring-slate-200">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-semibold">Vendas da Shopee por grupo</h2>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm">
          {[7, 30, 90].map((n) => (
            <option key={n} value={n}>
              últimos {n} dias
            </option>
          ))}
        </select>
      </div>
      <p className="text-xs text-slate-500">
        Cada grupo recebe um link próprio da Shopee; o relatório da Shopee diz de qual link veio o pedido. Pedidos levam até 24 h para aparecer e a
        comissão é estimada, não paga. Só conta ofertas enviadas depois de ligar o link por grupo.
      </p>
      {error && <ErrorBox message={error} />}
      {!data && !error && <Spinner />}
      {data && data.groups.length === 0 && <p className="text-sm text-slate-600">Escolha grupos acima para ver as vendas por grupo.</p>}
      {data && data.groups.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1">Grupo</th>
              <th>Pedidos</th>
              <th>Concluídos</th>
              <th>Pendentes</th>
              <th>Cancelados</th>
              <th className="text-right">Comissão</th>
            </tr>
          </thead>
          <tbody>
            {data.groups.map((g) => (
              <tr key={g.id} className="border-t border-slate-100">
                <td className="py-1 font-medium">{g.name}</td>
                <td>{g.conversions}</td>
                <td>{g.completed}</td>
                <td>{g.pending}</td>
                <td>{g.cancelled}</td>
                <td className="text-right">{money(g.commission)}</td>
              </tr>
            ))}
            {data.unattributed && data.unattributed.conversions > 0 && (
              <tr className="border-t border-slate-100 text-slate-500">
                <td className="py-1" title="Pedidos sem o identificador de grupo no link (links de antes ou grupo que saiu da lista)">
                  Sem grupo
                </td>
                <td>{data.unattributed.conversions}</td>
                <td>{data.unattributed.completed}</td>
                <td>{data.unattributed.pending}</td>
                <td>{data.unattributed.cancelled}</td>
                <td className="text-right">{money(data.unattributed.commission)}</td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function Sender() {
  const [overview, setOverview] = useState<SenderOverview | null>(null);
  const [jobs, setJobs] = useState<SenderJob[]>([]);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    Promise.all([api.sender(), api.senderJobs()])
      .then(([o, j]) => (setOverview(o), setJobs(j), setError("")))
      .catch((exc: Error) => setError(exc.message));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000); // o QR e a fila mudam sozinhos
    return () => clearInterval(timer);
  }, [load]);

  async function act(call: () => Promise<unknown>) {
    try {
      await call();
      load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Falhou.");
    }
  }

  if (!overview) return error ? <ErrorBox message={error} /> : <Spinner />;
  if (!overview.configured) {
    return (
      <p className="rounded-md bg-white p-6 text-sm text-slate-700 ring-1 ring-slate-200">
        O robô de envio está desligado. Defina <code>SENDER_URL</code> e <code>SENDER_TOKEN</code> no <code>.env</code> e suba o serviço{" "}
        <code>sender</code> (veja o README). Enquanto isso, use "Copiar mensagem".
      </p>
    );
  }

  const { connection } = overview;
  return (
    <div className="space-y-4">
      {error && <ErrorBox message={error} />}

      <section className="space-y-2 rounded-lg bg-white p-4 ring-1 ring-slate-200">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="font-semibold">WhatsApp: {STATE_LABEL[connection.state] ?? connection.state}</h2>
          {overview.paused ? (
            <button type="button" onClick={() => act(api.resumeSender)} className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600">
              Retomar envios
            </button>
          ) : (
            <button type="button" onClick={() => act(api.pauseSender)} className="rounded-md px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50">
              Pausar envios
            </button>
          )}
        </div>
        {overview.paused && <p className="text-sm text-amber-800">Pausado: {overview.pause_reason || "sem motivo registrado"}.</p>}
        {connection.error && <p className="text-sm text-rose-700">{connection.error}</p>}
        {connection.state === "qr" && connection.qr && (
          <div>
            <p className="text-sm text-slate-600">No celular do número do robô: WhatsApp → Aparelhos conectados → Conectar um aparelho.</p>
            <img src={connection.qr} alt="QR code do WhatsApp" className="mt-2 size-56" />
          </div>
        )}
        <p className="text-sm text-slate-600">
          Hoje: <b>{overview.sent_today}</b> de {overview.daily_cap} mensagens · {overview.queued} na fila · janela {overview.window[0]}h–{overview.window[1]}h (Brasília)
          {overview.worker.next_send_at && overview.queued > 0 && <> · próxima a partir de {dateTimeLabel(overview.worker.next_send_at)}</>}
        </p>
        {overview.worker.last_error && <p className="text-xs text-slate-500">Último aviso do robô: {overview.worker.last_error}</p>}
      </section>

      <AutoSendCard initial={overview.auto} onChanged={load} />

      <SalesByGroupCard />

      {connection.state === "ready" && <GroupPicker selected={overview.groups.map((g) => g.id)} onSaved={load} />}

      <section className="space-y-2">
        <h2 className="font-semibold">Fila e envios recentes</h2>
        {jobs.length === 0 ? (
          <p className="text-sm text-slate-600">Nada enviado ainda. Em "Ofertas", use "Enviar aos grupos" numa oferta pronta.</p>
        ) : (
          <ul className="space-y-1.5">
            {jobs.map((job) => (
              <li key={job.id} className="flex flex-wrap items-center gap-2 rounded-md bg-white p-2 text-sm ring-1 ring-slate-200">
                <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${JOB_STYLE[job.status]}`}>{JOB_LABEL[job.status]}</span>
                <span className="font-medium">{job.group}</span>
                <span className="min-w-0 flex-1 truncate text-slate-600" title={job.text}>
                  {job.text.split("\n")[0]} · {money(job.price)}
                </span>
                <span className="text-xs text-slate-500">{job.error ?? dateTimeLabel(job.sent_at ?? job.created_at)}</span>
                {job.status === "queued" && (
                  <button type="button" onClick={() => act(() => api.cancelSenderJob(job.id))} className="text-xs text-rose-700 hover:underline">
                    cancelar
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
