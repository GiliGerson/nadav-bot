// Demo dashboard: renders a saved snapshot of real watchlist data (site/data/watchlist.json).
const STANCE_ORDER = { Buy: 3, Hold: 2, Sell: 1 };
const STANCE_HE = { Buy: "קנייה", Hold: "החזקה", Sell: "מכירה" };
// Signal labels come from the bot's API in English; the page shows them in Hebrew.
const SIGNAL_HE = [
  [/^Above SMA 200$/, () => "מעל SMA 200"],
  [/^Below SMA 200$/, () => "מתחת ל-SMA 200"],
  [/^Averages stacked up$/, () => "ממוצעים בסדר עולה"],
  [/^Averages stacked down$/, () => "ממוצעים בסדר יורד"],
  [/^Oversold \(RSI (\d+)\)$/, (m) => `מכירת יתר (RSI ${m[1]})`],
  [/^Overbought \(RSI (\d+)\)$/, (m) => `קניית יתר (RSI ${m[1]})`],
  [/^Strong 3M momentum$/, () => "מומנטום חזק ב-3 חודשים"],
  [/^Weak 3M momentum$/, () => "מומנטום חלש ב-3 חודשים"],
  [/^Near 52-week high$/, () => "קרוב לשיא השנתי"],
  [/^Deep drawdown from high$/, () => "ירידה חדה מהשיא"],
  [/^Up on heavy volume$/, () => "עלייה בנפח מסחר גבוה"],
  [/^Down on heavy volume$/, () => "ירידה בנפח מסחר גבוה"],
];
const signalHe = (label) => {
  for (const [re, fmt] of SIGNAL_HE) {
    const m = label.match(re);
    if (m) return fmt(m);
  }
  return label;
};
let rows = [];
let sort = { key: "score", dir: -1 };

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const num = (v, d = 2) =>
  v == null ? "n/a" : v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v) => (v == null ? "n/a" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);
const pctCell = (v) => {
  const td = el("td", `num ${v == null ? "muted" : v >= 0 ? "up" : "down"}`);
  td.append(el("span", "ltr", pct(v)));
  return td;
};

function render() {
  const body = $("rows");
  body.replaceChildren();
  $("t-total").textContent = rows.length;
  for (const s of ["Buy", "Hold", "Sell"]) $(`t-${s}`).textContent = rows.filter((r) => r.stance === s).length;

  const sorted = [...rows].sort((a, b) => {
    const va = sort.key === "stance" ? STANCE_ORDER[a.stance] : a[sort.key];
    const vb = sort.key === "stance" ? STANCE_ORDER[b.stance] : b[sort.key];
    if (va == null) return 1;
    if (vb == null) return -1;
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sort.dir;
  });

  for (const r of sorted) {
    const tr = el("tr");
    const name = el("td");
    name.append(el("div", "name", r.name || r.ticker), el("div", "ticker", r.ticker));
    const price = el("td", "num");
    const priceText = el("span", "ltr", num(r.price));
    priceText.append(el("span", "muted", ` ${r.currency || ""}`));
    price.append(priceText);

    const score = el("td");
    score.style.whiteSpace = "nowrap";
    const meter = el("span", "meter");
    meter.setAttribute("aria-hidden", "true");
    for (let i = 1; i <= 5; i++) meter.append(el("i", i <= r.score ? "on" : ""));
    score.append(meter, el("span", "ltr", `${r.score}/5`));

    const stance = el("td");
    const badge = el("span", `stance stance-${r.stance}`);
    badge.append(el("span", "dot"), STANCE_HE[r.stance]);
    stance.append(badge);

    const sigs = el("td");
    const chips = el("div", "chips");
    for (const s of r.signals) {
      const chip = el("span", "chip");
      chip.title = s.bias > 0 ? "אות חיובי: מוסיף נקודה" : "אות שלילי: מוריד נקודה";
      chip.append(el("b", s.bias > 0 ? "up" : "down", s.bias > 0 ? "▲" : "▼"), signalHe(s.label));
      chips.append(chip);
    }
    sigs.append(chips);

    tr.append(name, price, pctCell(r.change_1d_pct), pctCell(r.change_3m_pct),
      el("td", "num", num(r.rsi_14, 1)), pctCell(r.from_52w_high_pct), score, stance, sigs);
    body.append(tr);
  }

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.querySelector(".arrow-ind").textContent =
      th.dataset.sort === sort.key ? (sort.dir > 0 ? " ↑" : " ↓") : "";
  });
}

document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    sort = sort.key === key ? { key, dir: -sort.dir } : { key, dir: key === "name" ? 1 : -1 };
    render();
  });
});

fetch("data/watchlist.json")
  .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
  .then((data) => {
    rows = data;
    render();
  })
  .catch(() => {
    $("rows").replaceChildren();
    const td = el("td", "muted", "לא הצלחנו לטעון את נתוני ההדגמה.");
    td.colSpan = 9;
    const tr = el("tr");
    tr.append(td);
    $("rows").append(tr);
  });
