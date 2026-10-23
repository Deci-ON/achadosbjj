import { useState } from "react";
import { Link } from "react-router";
import { ApiError, api, type Offer, type Status } from "./api";
import { STATUS_HINT, STATUS_STYLE } from "./format";

export function StatusBadge({ status, label }: { status: Status; label: string }) {
  return (
    <span
      title={STATUS_HINT[status]}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[status]}`}
    >
      {label}
    </span>
  );
}

export function ScoreBadge({ score }: { score: number }) {
  const tone = score >= 80 ? "bg-emerald-600" : score >= 60 ? "bg-lime-600" : score >= 40 ? "bg-amber-500" : "bg-slate-400";
  return (
    <span title="Pontuação de 0 a 100: queda real, menor preço do período, histórico" className={`inline-flex min-w-9 justify-center rounded-md px-1.5 py-0.5 text-xs font-bold text-white ${tone}`}>
      {score}
    </span>
  );
}

// navigator.clipboard só existe em contexto seguro (HTTPS ou localhost). Acessando o painel por http://IP
// ele não existe, então o plano B é um textarea escondido com execCommand.
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* cai no plano B */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}

export function CopyButton({ text, label = "Copiar mensagem" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");

  async function onClick() {
    setState((await copyText(text)) ? "ok" : "fail");
    setTimeout(() => setState("idle"), 2000);
  }

  const styles =
    state === "ok"
      ? "bg-emerald-600 text-white"
      : state === "fail"
        ? "bg-rose-600 text-white"
        : "bg-slate-900 text-white hover:bg-slate-700";
  return (
    <button type="button" onClick={onClick} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${styles}`}>
      {state === "ok" ? "Copiado ✓" : state === "fail" ? "Não foi possível copiar" : label}
    </button>
  );
}

export function Spinner({ label = "Carregando…" }: { label?: string }) {
  return <p className="py-8 text-center text-sm text-slate-500">{label}</p>;
}

export function ErrorBox({ message }: { message: string }) {
  return <p className="rounded-md bg-rose-50 p-3 text-sm text-rose-800 ring-1 ring-rose-200">{message}</p>;
}

/** "Marcar como enviada" / "Desfazer": a oferta enviada sai de "prontas" por 7 dias (ou até o preço cair mais de 3%). */
export function SentToggle({ offer, onChanged }: { offer: Offer; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sent = offer.sent_at !== null;

  async function toggle() {
    setBusy(true);
    setError("");
    try {
      await (sent ? api.unmarkSent(offer.id) : api.markSent(offer.id));
      onChanged();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Não consegui salvar.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-col">
      <button
        type="button"
        onClick={toggle}
        disabled={busy}
        className={`rounded-md px-3 py-1.5 text-sm font-medium ring-1 disabled:opacity-50 ${
          sent ? "bg-emerald-50 text-emerald-800 ring-emerald-200 hover:bg-emerald-100" : "text-slate-700 ring-slate-300 hover:bg-slate-50"
        }`}
        title={sent ? "Clique para desfazer" : "Tira das prontas por 7 dias, a menos que o preço caia mais de 3%"}
      >
        {sent ? "Enviada ✓ desfazer" : "Marcar como enviada"}
      </button>
      {error && <span className="mt-1 text-xs text-rose-700">{error}</span>}
    </span>
  );
}

/** Aviso de ofertas do ML esperando o link de afiliado. */
export function PendingLinksBanner({ pending }: { pending: number }) {
  if (pending <= 0) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-amber-50 p-3 text-sm text-amber-900 ring-1 ring-amber-200">
      <span>
        <b>{pending}</b> {pending === 1 ? "oferta aguarda" : "ofertas aguardam"} o link de afiliado para ficar pronta para enviar.
      </span>
      <Link to="/links" className="rounded-md bg-amber-900 px-3 py-1.5 font-medium text-white hover:bg-amber-800">
        Gerar links
      </Link>
    </div>
  );
}
