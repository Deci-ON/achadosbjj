export type Status = "offer" | "no_offer" | "learning" | "irrelevant";

export interface Offer {
  id: number;
  source: string;
  external_id: string;
  title: string;
  url: string;
  affiliate_url: string | null; // o que vai na mensagem
  affiliate_origin: "manual" | "api" | null;
  image_url: string | null;
  price: number;
  original_price: number | null;
  status: Status;
  score: number;
  reference_price: number | null;
  real_discount_pct: number | null;
  claimed_discount_pct: number | null;
  observed_days: number;
  reasons: string[];
  last_seen_at: string;
  message: string;
  ready: boolean; // oferta com link de afiliado e ainda não enviada
  needs_link: boolean; // oferta do ML sem link: está na fila
  sent_at: string | null; // último envio dentro da carência de 7 dias
}

export interface PricePoint {
  price: number;
  original_price: number | null;
  collected_at: string;
}

export interface ProductDetail extends Offer {
  history: PricePoint[];
}

export interface OffersResponse {
  counts: Record<Status, number>;
  ready: number;
  pending_link: number;
  below_cut: number; // ofertas do ML sem link que ficaram de fora da fila pelo corte de pontuação
  items: Offer[];
}

export interface PendingItem {
  id: number;
  title: string;
  url: string;
  price: number;
  score: number;
}

export interface PendingResponse {
  total: number;
  min_score: number; // corte de pontuação da fila (LINK_MIN_SCORE)
  below_cut: number;
  items: PendingItem[];
}

export interface Run {
  id: number;
  source: string;
  status: "running" | "ok" | "error";
  started_at: string;
  finished_at: string | null;
  items_seen: number;
  items_new: number;
  error: string | null;
}

export interface StatusResponse {
  scheduler: {
    enabled: boolean;
    is_leader: boolean;
    running: boolean;
    consecutive_failures: number;
    last_error: string | null;
    next_due: string | null;
  };
  products: Record<string, number>;
  recent_runs: Run[];
}

const TOKEN_KEY = "achadosbjj.token";

// localStorage pode lançar (janela privada, dados bloqueados): o painel funciona sem ele, só pede o token de novo.
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* sem armazenamento: vale só nesta aba */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Avisa o App de que o token não vale mais, para voltar à tela de login.
export const UNAUTHORIZED_EVENT = "achadosbjj:unauthorized";

async function request<T>(path: string, init: RequestInit = {}, token = getToken()): Promise<T> {
  const response = await fetch(path, { ...init, headers: { ...init.headers, Authorization: `Bearer ${token}` } });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* corpo não é JSON */
    }
    if (response.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export interface OffersQuery {
  status: Status | "ready"; // "ready" = prontas para enviar
  q?: string;
  minScore?: number;
  limit?: number;
}

export const api = {
  /** Confere o token antes de guardá-lo. */
  check: (token: string) => request<StatusResponse>("/api/status", {}, token),
  status: () => request<StatusResponse>("/api/status"),
  offers: ({ status, q, minScore = 0, limit = 60 }: OffersQuery) => {
    const params = new URLSearchParams({ limit: String(limit), min_score: String(minScore) });
    if (status === "ready") params.set("ready", "true");
    else params.set("status", status);
    if (q?.trim()) params.set("q", q.trim());
    return request<OffersResponse>(`/api/offers?${params}`);
  },
  product: (id: number) => request<ProductDetail>(`/api/products/${id}`),
  /** url vazia remove o link guardado. */
  setAffiliateLink: (id: number, url: string | null) =>
    request<ProductDetail>(`/api/products/${id}/affiliate-link`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),
  pendingLinks: (limit = 20) => request<PendingResponse>(`/api/affiliate/pending?limit=${limit}`),
  /** Os links na mesma ordem das URLs coladas no gerador do ML. Tudo ou nada. */
  saveLinks: (items: { id: number; url: string }[]) =>
    request<{ saved: number }>("/api/affiliate/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    }),
  markSent: (id: number) => request<ProductDetail>(`/api/products/${id}/sent`, { method: "POST" }),
  unmarkSent: (id: number) => request<ProductDetail>(`/api/products/${id}/sent`, { method: "DELETE" }),
  collect: () => request<{ status: string }>("/api/collect", { method: "POST" }),
};
