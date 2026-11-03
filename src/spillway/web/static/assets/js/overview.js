// UI-1 總覽(CHG-20261103-01 T4;決策 11、18、20;設計差異 #2、#3)。
// - `GET /api/settings` 的 first_run 為 true 時導向精靈;`?skip_wizard=1` 該次不導向。
// - counts.deployments > 0 且 counts.routes == 0 時顯示續作橫幅(連到 wizard.html)。
// - 摘要卡:冷卻中成員數、近 24 小時請求數(取自 /api/dashboard)。
// - 每個成員一列:id(連到 members.html#<id>)、tags、類型、額度類型、本期使用、剩餘(USD;unlimited 為「不限」)、
//   預估可用天數、近 7 天日均、近 24 小時請求數、狀態(冷卻至 <本地時間> 與原因);冷卻中的列有「清除冷卻」。
// 動態資料只經 h() / textContent 放入 DOM(決策 8)。

import { getJSON, sendJSON } from "./api.js";
import { byId, clear, h, setText } from "./dom.js";
import { BUDGET_TYPE, COOLING_REASON, TEXT, WINDOW, label } from "./strings.js";
import { codeLabel, fmtLocal, fmtUSD, initShell, kindBadge, latest, memberHref, navigate, showError, showMessage,
  timeEl, withBusy } from "./ui.js";

const T = Object.freeze({
  days: (n) => `${n} 天`,
  underOneDay: "< 1 天",
  usedPct: (pct) => `已使用 ${pct}%`,
  clearCooldown: "清除冷卻",
  cooldownCleared: (id) => `已清除 ${id} 的冷卻`,
  noMembers: "尚未加入成員",
  noBudget: "未設額度",
  noneCooling: "目前沒有冷卻中的成員",
  coolingList: (ids) => `冷卻中:${ids.join("、")}`,
  generatedAt: "資料時間 ",
  memberLink: (id) => `前往成員 ${id}`,
});

function wantsSkipWizard() {
  return new URLSearchParams(window.location.search).get("skip_wizard") === "1";
}

function budgetCell(row) {
  if (!row.budget_type) return h("span", { class: "muted" }, T.noBudget);
  const parts = [label(BUDGET_TYPE, row.budget_type)];
  if (row.window) parts.push(label(WINDOW, row.window));
  return h("span", { title: [row.budget_type, row.window].filter(Boolean).join(" · ") }, parts.join(" · "));
}

// 設計稿的進度條以原生 <meter> 呈現(只有 min/max/value 屬性,不需要 style;決策 7、8)
function usageCell(row) {
  if (row.unlimited || row.limit_amount === null || row.limit_amount === undefined) {
    return h("span", {}, fmtUSD(row.period_spend));
  }
  const limit = Number(row.limit_amount);
  const spend = Number(row.period_spend) || 0;
  const pct = limit > 0 ? Math.round((spend / limit) * 100) : 100;
  return h("div", { class: "overview-usage" },
    h("meter", { class: "overview-meter", min: "0", max: String(limit), value: String(spend),
      "aria-label": T.usedPct(pct) }),
    h("span", {}, `${fmtUSD(spend)} / ${fmtUSD(limit)}`));
}

function remainingCell(row) {
  if (row.unlimited) return h("span", { class: "muted", "data-field": "remaining" }, TEXT.unlimited);
  return h("span", { "data-field": "remaining" }, fmtUSD(row.remaining));
}

function daysCell(row) {
  if (row.est_days_left === null || row.est_days_left === undefined) return h("span", { class: "muted" }, TEXT.dash);
  const days = Number(row.est_days_left);
  if (days > 0 && days < 1) return h("span", {}, T.underOneDay);
  return h("span", {}, T.days(Math.floor(days)));
}

function statusCell(row) {
  if (row.cooling) {
    return h("span", { class: "badge b-warn", "data-field": "cooling" },
      `${TEXT.cooling_until} `, timeEl(row.cooling_until), "(",
      codeLabel(COOLING_REASON, row.cooling_reason), ")");
  }
  if (!row.enabled) return h("span", { class: "badge b-muted" }, TEXT.disabled);
  return h("span", { class: "badge b-ok" }, TEXT.normal);
}

function memberCell(row, dep) {
  const tags = dep && Array.isArray(dep.tags) ? dep.tags : [];
  return h("div", { class: "stack tight" },
    h("a", { class: "mono overview-member-link", href: memberHref(row.id), title: T.memberLink(row.id) }, row.id),
    tags.length ? h("div", { class: "chips tight overview-tags" }, ...tags.map((t) => h("span", { class: "chip" }, t))) : null);
}

function renderRows(rows, deps, reload) {
  const body = byId("members-body");
  clear(body);
  if (!rows.length) {
    body.append(h("tr", {}, h("td", { colspan: "10", class: "empty" }, T.noMembers)));
    return;
  }
  for (const row of rows) {
    let action = null;
    if (row.cooling) {
      const btn = h("button", { type: "button", class: "btn sm", "data-action": "clear-cooldown" }, T.clearCooldown);
      btn.addEventListener("click", () => withBusy(btn, async () => {
        try {
          await sendJSON("POST", `/api/deployments/${encodeURIComponent(row.id)}/clear-cooldown`);
          showMessage(T.cooldownCleared(row.id), "ok");
          await reload();
        } catch (err) {
          showError(err);
        }
      }));
      action = btn;
    }
    body.append(h("tr", { "data-member": row.id },
      h("td", {}, memberCell(row, deps.get(row.id))),
      h("td", {}, kindBadge(row.kind)),
      h("td", {}, budgetCell(row)),
      h("td", { class: "num" }, usageCell(row)),
      h("td", { class: "num" }, remainingCell(row)),
      h("td", { class: "num" }, daysCell(row)),
      h("td", { class: "num", "data-field": "daily" }, fmtUSD(row.daily_spend_7d)),
      h("td", { class: "num", "data-field": "requests" }, String(row.requests_24h)),
      h("td", {}, statusCell(row)),
      h("td", {}, action)));
  }
}

function renderSummary(rows) {
  const cooling = rows.filter((r) => r.cooling);
  setText(byId("stat-cooling"), cooling.length);
  setText(byId("stat-cooling-note"), cooling.length ? T.coolingList(cooling.map((r) => r.id)) : T.noneCooling);
  setText(byId("stat-requests"), rows.reduce((sum, r) => sum + Number(r.requests_24h || 0), 0));
}

const dashGuard = latest();

async function loadDashboard() {
  const isLatest = dashGuard();
  const [dash, depsResp] = await Promise.all([getJSON("/api/dashboard"), getJSON("/api/deployments")]);
  if (!isLatest()) return;      // 較新的一次載入已在進行,舊回應不渲染
  const rows = Array.isArray(dash.deployments) ? dash.deployments : [];
  const deps = new Map((depsResp.deployments || []).map((d) => [d.id, d]));
  renderSummary(rows);
  renderRows(rows, deps, loadDashboard);
  setText(byId("generated-at"), dash.generated_at ? `${T.generatedAt}${fmtLocal(dash.generated_at)}` : "");
  byId("members-table").setAttribute("data-loaded", "true");
}

async function main() {
  initShell();
  try {
    const settings = await getJSON("/api/settings");
    if (settings.first_run && !wantsSkipWizard()) {
      // 換掉目前的歷史項:從精靈按返回鍵不會再回到這裡被導向一次
      navigate("/ui/wizard.html", { replace: true });
      return;
    }
    const counts = settings.counts || {};
    byId("resume-banner").hidden = !(counts.deployments > 0 && counts.routes === 0);
    await loadDashboard();
  } catch (err) {
    showError(err);
  }
}

main();
