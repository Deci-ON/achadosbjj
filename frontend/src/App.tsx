import { Suspense, lazy, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router";
import { UNAUTHORIZED_EVENT, getToken, setToken } from "./api";
import { Dashboard } from "./pages/Dashboard";
import { Login } from "./pages/Login";
import { Offers } from "./pages/Offers";
import { Spinner } from "./components";

// A página de produto carrega o Recharts (a maior parte do JavaScript): só baixa quando alguém abre um produto.
const Product = lazy(() => import("./pages/Product").then((m) => ({ default: m.Product })));

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm font-medium ${isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-200"}`;

export default function App() {
  const [authed, setAuthed] = useState(() => getToken() !== "");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const onUnauthorized = () => {
      setToken("");
      setNotice("Sessão expirada ou token inválido. Entre de novo.");
      setAuthed(false);
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (!authed) {
    return <Login notice={notice} onLogin={() => (setNotice(""), setAuthed(true))} />;
  }

  return (
    <BrowserRouter>
      <header className="border-b border-slate-200 bg-white">
        <nav className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-2">
          <span className="mr-4 font-bold">achadosbjj</span>
          <NavLink to="/" end className={linkClass}>
            Painel
          </NavLink>
          <NavLink to="/ofertas" className={linkClass}>
            Ofertas
          </NavLink>
          <button
            type="button"
            onClick={() => (setToken(""), setAuthed(false))}
            className="ml-auto rounded-md px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-200"
          >
            Sair
          </button>
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/ofertas" element={<Offers />} />
          <Route
            path="/produto/:id"
            element={
              <Suspense fallback={<Spinner />}>
                <Product />
              </Suspense>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
