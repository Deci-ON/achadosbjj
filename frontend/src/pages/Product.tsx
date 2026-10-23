import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AffiliateCard } from "../AffiliateCard";
import { ApiError, api, type ProductDetail } from "../api";
import { CopyButton, ErrorBox, ScoreBadge, Spinner, StatusBadge } from "../components";
import { SOURCE_LABEL, STATUS_LABEL, dateTimeLabel, money, parseDate, shortDateLabel } from "../format";

function PriceChart({ product }: { product: ProductDetail }) {
  const data = useMemo(() => {
    const points = product.history.map((p) => ({ t: parseDate(p.collected_at).getTime(), price: p.price }));
    // O preço vale até a próxima mudança: prolonga a última linha até a última vez que o produto foi visto.
    const lastSeen = parseDate(product.last_seen_at).getTime();
    const last = points[points.length - 1];
    if (last && lastSeen > last.t) points.push({ t: lastSeen, price: last.price });
    return points;
  }, [product]);

  if (data.length < 2) {
    return <p className="text-sm text-slate-600">Histórico ainda curto: {product.history.length} ponto de preço registrado.</p>;
  }
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={shortDateLabel} fontSize={12} />
          <YAxis domain={["auto", "auto"]} tickFormatter={(v: number) => `R$ ${v}`} width={70} fontSize={12} />
          <Tooltip
            formatter={(value: number) => [money(value), "Preço"]}
            labelFormatter={(label: number) => dateTimeLabel(new Date(label).toISOString())}
          />
          {product.reference_price != null && (
            <ReferenceLine
              y={product.reference_price}
              stroke="#64748b"
              strokeDasharray="5 5"
              label={{ value: "preço normal", position: "insideTopRight", fontSize: 11, fill: "#64748b" }}
            />
          )}
          <Line type="stepAfter" dataKey="price" stroke="#059669" strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function Product() {
  const { id } = useParams();
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setProduct(null);
    api
      .product(Number(id))
      .then((body) => !cancelled && setProduct(body))
      .catch((exc: unknown) => !cancelled && setError(exc instanceof ApiError && exc.status === 404 ? "Produto não encontrado." : "Não consegui carregar o produto."));
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error) return <ErrorBox message={error} />;
  if (!product) return <Spinner />;

  return (
    <div className="space-y-4">
      <Link to="/ofertas" className="text-sm text-slate-600 underline">
        ← Voltar às ofertas
      </Link>

      <section className="flex gap-4 rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
        {product.image_url && <img src={product.image_url} alt="" className="size-28 shrink-0 rounded-md object-contain" />}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={product.status} label={STATUS_LABEL[product.status]} />
            <ScoreBadge score={product.score} />
            <span className="text-xs text-slate-500">{SOURCE_LABEL[product.source] ?? product.source}</span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{product.title}</h1>
          <p className="mt-1">
            <span className="text-2xl font-bold">{money(product.price)}</span>
            {product.status === "offer" && product.reference_price != null && (
              <span className="ml-3 text-slate-600">
                <b className="text-emerald-700">−{product.real_discount_pct}%</b> sobre {money(product.reference_price)}
              </span>
            )}
          </p>
          <ul className="mt-2 list-disc pl-5 text-sm text-slate-600">
            {product.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      </section>

      <section className="rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Histórico de preço</h2>
        <PriceChart product={product} />
        <p className="mt-2 text-xs text-slate-500">
          {product.observed_days} dias observados.
          {product.claimed_discount_pct != null && ` O vendedor declara ${product.claimed_discount_pct}% de desconto, mas isso não entra na conta.`}
        </p>
      </section>

      <AffiliateCard product={product} onChange={setProduct} />

      <section className="rounded-lg bg-white p-4 shadow-sm ring-1 ring-slate-200">
        <div className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Mensagem para o WhatsApp</h2>
          <CopyButton text={product.message} />
        </div>
        <pre className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm ring-1 ring-slate-200">{product.message}</pre>
      </section>
    </div>
  );
}
