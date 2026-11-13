export type Status = "offer" | "no_offer" | "learning" | "irrelevant";

export interface Variant {
  id: number;
  size: string;
  price: number;
  status: Status;
  score: number;
}

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
  variants: Variant[]; // os tamanhos do modelo; vazio quando o produto não tem tamanhos
  coupons?: OfferCoupon[]; // os cupons que estão na mensagem agora (opcional: backend antigo não manda)
  attached_coupon_ids?: number[]; // os cupons anexados a esta oferta
}

export interface OfferCoupon {
  id: number;
  title: string;
  kind: string;
}

export type CouponStore = "mercadolivre" | "shopee" | "all";
export type CouponKind = "code" | "page" | "shipping";

export interface Coupon {
  id: number;
  store: CouponStore;
  kind: CouponKind;
  title: string;
  rule: string;
  expires_at: string;
  is_default: boolean;
  active: boolean; // não venceu
  created_at: string;
}

export interface CouponInput {
  store: CouponStore;
  kind: CouponKind;
  title: string;
  rule?: string;
  expires_at: string;
  is_default?: boolean;
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
  by_source?: Record<string, number>; // ofertas da aba atual por loja, sem o filtro de loja (ausente num backend antigo)
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

export interface SchedulerState {
  enabled: boolean;
  is_leader: boolean;
  running: boolean;
  consecutive_failures: number;
  last_error: string | null;
  next_due: string | null;
}

export interface StatusResponse {
  scheduler: SchedulerState;
  shopee_scheduler: SchedulerState | null; // null = coleta da Shopee não está ligada
  products: Record<string, number>;
  recent_runs: Run[];
}

export interface SenderOverview {
  configured: boolean; // SENDER_URL definido
  connection: { state: string; qr: string | null; error?: string }; // state: off | unreachable | starting | qr | authenticated | ready | disconnected
  paused: boolean;
  pause_reason: string;
  daily_cap: number;
  sent_today: number;
  queued: number;
  window: [number, number]; // janela de envio (horário de Brasília)
  groups: { id: string; name: string }[]; // os grupos que recebem as ofertas
  worker: { is_leader: boolean; last_error: string | null; next_send_at: string | null };
  auto?: AutoSend; // ausente num backend que ainda não tem o envio automático
}

export interface AutoSend {
  enabled: boolean;
  min_score: number;
}

export interface AutoSendSettings extends AutoSend {
  default_min_score: number;
}

export interface GroupSales {
  id?: string;
  name?: string;
  tag?: string;
  conversions: number; // pedidos feitos pelo link
  commission: number; // comissão estimada (R$)
  completed: number;
  pending: number;
  cancelled: number;
}

export interface SenderPerformance {
  configured: boolean; // Shopee ligada (SHOPEE_APP_ID e SHOPEE_SECRET)
  days: number;
  conversions: number;
  groups: GroupSales[];
  unattributed: GroupSales | null; // vendas sem sub-id de grupo (links de antes, grupo tirado da lista)
}

export interface SenderGroup {
  id: string;
  name: string;
  participants: number;
  selected: boolean;
}

export interface SenderJob {
  id: number;
  product_id: number;
  group: string;
  status: "queued" | "sent" | "failed" | "cancelled";
  price: number;
  attempts: number;
  error: string | null;
  created_at: string;
  sent_at: string | null;
  text: string;
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
  source?: string; // "mercadolivre" | "shopee"; vazio = todas as lojas
  q?: string;
  minScore?: number;
  limit?: number;
}

export const api = {
  /** Confere o token antes de guardá-lo. */
  check: (token: string) => request<StatusResponse>("/api/status", {}, token),
  status: () => request<StatusResponse>("/api/status"),
  offers: ({ status, source, q, minScore = 0, limit = 60 }: OffersQuery) => {
    const params = new URLSearchParams({ limit: String(limit), min_score: String(minScore) });
    if (status === "ready") params.set("ready", "true");
    else params.set("status", status);
    if (source) params.set("source", source);
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
  sender: () => request<SenderOverview>("/api/sender"),
  senderAuto: () => request<AutoSendSettings>("/api/sender/auto"),
  /** minScore null mantém o corte atual. */
  setSenderAuto: (enabled: boolean, minScore: number | null) =>
    request<AutoSendSettings>("/api/sender/auto", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled, min_score: minScore }),
    }),
  senderGroups: () => request<SenderGroup[]>("/api/sender/groups"),
  setSenderGroups: (groups: { id: string; name: string }[]) =>
    request<{ groups: number }>("/api/sender/groups", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ groups }),
    }),
  pauseSender: () => request<{ paused: boolean }>("/api/sender/pause", { method: "POST" }),
  resumeSender: () => request<{ paused: boolean }>("/api/sender/resume", { method: "POST" }),
  senderJobs: () => request<SenderJob[]>("/api/sender/jobs"),
  senderPerformance: (days = 30) => request<SenderPerformance>(`/api/sender/performance?days=${days}`),
  cancelSenderJob: (id: number) => request<{ cancelled: number }>(`/api/sender/jobs/${id}`, { method: "DELETE" }),
  coupons: () => request<Coupon[]>("/api/coupons"),
  createCoupon: (body: CouponInput) =>
    request<Coupon>("/api/coupons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateCoupon: (id: number, body: Partial<CouponInput>) =>
    request<Coupon>(`/api/coupons/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteCoupon: (id: number) => request<{ deleted: number }>(`/api/coupons/${id}`, { method: "DELETE" }),
  /** Substitui os cupons anexados à oferta. */
  setProductCoupons: (id: number, couponIds: number[]) =>
    request<{ coupon_ids: number[] }>(`/api/products/${id}/coupons`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ coupon_ids: couponIds }),
    }),
  /** Põe a oferta pronta na fila do robô (um job por grupo escolhido). */
  sendOffer: (id: number) => request<{ queued: number }>(`/api/products/${id}/send`, { method: "POST" }),
};
