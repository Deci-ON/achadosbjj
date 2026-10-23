import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { ApiError, api, type OffersResponse, type Status, type StatusResponse } from "../api";
import { ErrorBox, Spinner } from "../components";
import { SOURCE_LABEL, STATUS_HINT, STATUS_LABEL, dateTimeLabel, duration, relative } from "../format";

const STATUSES: Status[] = ["offer", "learning", "no_offer", "irrelevant"];

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
      <div className="mt-2">{children}</div>
    </section>
  );
}

export function Dashboard() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [counts, setCounts] = useState<OffersResponse["counts"] | null>(null);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, o] = await Promise.all([api.status(), api.offers({ status: "offer", limit: 1 })]);
      setStatus(s);
      setCounts(o.counts);
      setError("");
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui falar com o servidor.");
    }
  }, []);

  const running = status?.scheduler.running ?? false;
  useEffect(() => {
    void load();
    // Enquanto uma coleta roda, olha de 5 em 5 segundos; fora disso, de 30 em 30.
    const timer = setInterval(() => void load(), running ? 5000 : 30000);
    return () => clearInterval(timer);
  }, [load, running]);

  async function collectNow() {
    setFeedback("");
    try {
      await api.collect();
      setFeedback("Coleta iniciada.");
      void load();
    } catch (exc) {
      setFeedback(exc instanceof ApiError ? exc.message : "Falhou ao iniciar a coleta.");
    }
  }

  if (!status && !error) return <Spinner />;
  if (!status) return <ErrorBox message={error} />;

  const { scheduler, products, recent_runs: runs } = status;
  const last = runs[0];
  const total = Object.values(products).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-4">
      {error && <ErrorBox message={error} />}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card title="Coleta">
          <p className="text-lg font-semibold">
            {!scheduler.enabled ? "Desligada" : scheduler.running ? "Rodando agora…" : "Ociosa"}
          </p>
          <p className="text-sm text-slate-600">
            {scheduler.enabled ? (scheduler.is_leader ? "Este servidor coleta" : "Aguardando liderança") : "COLLECT_ENABLED=false"}
          </p>
          {scheduler.enabled && !scheduler.running && <p className="mt-1 text-sm text-slate-600">Próxima: {relative(scheduler.next_due)}</p>}
          <button
            type="button"
            onClick={collectNow}
            disabled={!scheduler.enabled || scheduler.running}
            className="mt-3 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
          >
            Coletar agora
          </button>
          {feedback && <p className="mt-2 text-xs text-slate-600">{feedback}</p>}
        </Card>

        <Card title="Última coleta">
          {last ? (
            <>
              <p className={`text-lg font-semibold ${last.status === "error" ? "text-rose-700" : ""}`}>
                {last.status === "ok" ? "Concluída" : last.status === "running" ? "Em andamento" : "Com erro"}
              </p>
              <p className="text-sm text-slate-600">{relative(last.started_at)}</p>
              <p className="text-sm text-slate-600">
                {last.items_seen} vistos, {last.items_new} novos
              </p>
            </>
          ) : (
            <p className="text-sm text-slate-600">Nenhuma ainda.</p>
          )}
          {scheduler.consecutive_failures > 0 && (
            <p className="mt-2 text-sm text-rose-700">
              {scheduler.consecutive_failures} falha(s) seguida(s): {scheduler.last_error}
            </p>
          )}
        </Card>

        <Card title="Produtos">
          <p className="text-lg font-semibold">{total}</p>
          <ul className="text-sm text-slate-600">
            {Object.entries(products).map(([source, n]) => (
              <li key={source}>
                {SOURCE_LABEL[source] ?? source}: {n}
              </li>
            ))}
          </ul>
        </Card>

        <Card title="Ofertas agora">
          <p className="text-lg font-semibold">{counts?.offer ?? "—"}</p>
          <p className="text-sm text-slate-600">{counts?.learning ?? 0} ainda aprendendo</p>
          <Link to="/ofertas" className="mt-2 inline-block text-sm font-medium text-slate-900 underline">
            Ver ofertas
          </Link>
        </Card>
      </div>

      {counts && (
        <Card title="Situação dos produtos">
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {STATUSES.map((s) => (
              <li key={s}>
                <Link to={`/ofertas?status=${s}`} className="block rounded-md p-2 hover:bg-slate-50" title={STATUS_HINT[s]}>
                  <span className="text-xl font-semibold">{counts[s]}</span>
                  <span className="ml-2 text-sm text-slate-600">{STATUS_LABEL[s]}</span>
                </Link>
              </li>
            ))}
          </ul>
          {counts.learning > 0 && counts.offer === 0 && (
            <p className="mt-2 text-sm text-slate-600">
              As ofertas só aparecem depois de uns 7 dias de histórico de preço: antes disso o sistema não opina.
            </p>
          )}
        </Card>
      )}

      <Card title="Últimas coletas">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-500">
              <tr>
                <th className="py-1 pr-4">Início</th>
                <th className="py-1 pr-4">Duração</th>
                <th className="py-1 pr-4">Resultado</th>
                <th className="py-1 pr-4">Vistos</th>
                <th className="py-1 pr-4">Novos</th>
                <th className="py-1">Erro</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} className="border-t border-slate-100">
                  <td className="py-1 pr-4">{dateTimeLabel(run.started_at)}</td>
                  <td className="py-1 pr-4">{duration(run.started_at, run.finished_at)}</td>
                  <td className="py-1 pr-4">{run.status}</td>
                  <td className="py-1 pr-4">{run.items_seen}</td>
                  <td className="py-1 pr-4">{run.items_new}</td>
                  <td className="py-1 text-rose-700">{run.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
