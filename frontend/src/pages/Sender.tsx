import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type SenderGroup, type SenderJob, type SenderOverview } from "../api";
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
