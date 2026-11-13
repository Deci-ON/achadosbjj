import type { Status } from "./api";

const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const dateTime = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });
const shortDate = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" });

// O backend manda UTC; sem sufixo de fuso o navegador leria como horário local e a data sairia deslocada.
export function parseDate(iso: string): Date {
  return new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`);
}

// A validade é um dia do calendário de Brasília: formatar nesse fuso evita sair um dia antes em outro fuso.
const brDate = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "America/Sao_Paulo" });
export const dateLabel = (iso: string) => brDate.format(parseDate(iso));

export const money = (value: number | null | undefined) => (value == null ? "—" : brl.format(value));
export const dateTimeLabel = (iso: string | null) => (iso ? dateTime.format(parseDate(iso)) : "—");
export const shortDateLabel = (ms: number) => shortDate.format(new Date(ms));

export function relative(iso: string | null, now = Date.now()): string {
  if (!iso) return "—";
  const minutes = Math.round((parseDate(iso).getTime() - now) / 60000);
  const abs = Math.abs(minutes);
  if (abs < 1) return "agora";
  const unit = abs >= 1440 ? `${Math.round(abs / 1440)} d` : abs >= 60 ? `${Math.round(abs / 60)} h` : `${abs} min`;
  return minutes >= 0 ? `em ${unit}` : `há ${unit}`;
}

export function duration(start: string, end: string | null): string {
  if (!end) return "em andamento";
  const seconds = Math.round((parseDate(end).getTime() - parseDate(start).getTime()) / 1000);
  return seconds >= 60 ? `${Math.floor(seconds / 60)} min ${seconds % 60} s` : `${seconds} s`;
}

export const STATUS_LABEL: Record<Status, string> = {
  offer: "Ofertas",
  no_offer: "Sem oferta",
  learning: "Aprendendo",
  irrelevant: "Fora do nicho",
};

export const STATUS_HINT: Record<Status, string> = {
  offer: "Queda de 15% ou mais sobre o preço normal e menor preço do período",
  no_offer: "Preço dentro do normal, ou já esteve mais barato",
  learning: "Menos de 7 dias de histórico: ainda não dá para opinar",
  irrelevant: "Título não é de jiu-jitsu (judô, caratê, acessórios...)",
};

export const STATUS_STYLE: Record<Status, string> = {
  offer: "bg-emerald-100 text-emerald-800 ring-emerald-200",
  no_offer: "bg-slate-100 text-slate-700 ring-slate-200",
  learning: "bg-amber-100 text-amber-800 ring-amber-200",
  irrelevant: "bg-rose-50 text-rose-700 ring-rose-200",
};

export const SOURCE_LABEL: Record<string, string> = { mercadolivre: "Mercado Livre", shopee: "Shopee" };

// Uma cor por loja (selo, borda do card e aba): amarelo do Mercado Livre, laranja da Shopee.
export const SOURCE_STYLE: Record<string, { badge: string; border: string; tab: string }> = {
  mercadolivre: {
    badge: "bg-yellow-100 text-yellow-900 ring-yellow-300",
    border: "border-l-yellow-400",
    tab: "bg-yellow-400 text-slate-900 ring-yellow-400",
  },
  shopee: {
    badge: "bg-orange-100 text-orange-900 ring-orange-300",
    border: "border-l-orange-500",
    tab: "bg-orange-500 text-white ring-orange-500",
  },
};

export const COUPON_STORE_LABEL: Record<string, string> = { ...SOURCE_LABEL, all: "Ambas as lojas" };
export const COUPON_KIND_LABEL: Record<string, string> = {
  code: "Código para digitar",
  page: "Cupom da página de cupons",
  shipping: "Frete grátis",
};
