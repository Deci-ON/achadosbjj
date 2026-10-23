import { useState, type FormEvent } from "react";
import { ApiError, api, setToken } from "../api";

export function Login({ onLogin, notice }: { onLogin: () => void; notice?: string }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState(notice ?? "");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.check(value.trim());
      setToken(value.trim());
      onLogin();
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 503) setError("O servidor está sem ADMIN_TOKEN configurado.");
      else if (exc instanceof ApiError && exc.status === 401) setError("Token inválido.");
      else setError("Não consegui falar com o servidor.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-4">
      <h1 className="text-2xl font-bold">achadosbjj</h1>
      <p className="mt-1 text-sm text-slate-600">Cole o ADMIN_TOKEN do .env para entrar.</p>
      <form onSubmit={submit} className="mt-6 space-y-3">
        <input
          type="password"
          autoFocus
          autoComplete="current-password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="ADMIN_TOKEN"
          className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        {error && <p className="text-sm text-rose-700">{error}</p>}
        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {busy ? "Entrando…" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
