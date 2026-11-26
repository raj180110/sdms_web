// Frontend <-> backend integration: pages fetch JSON from the Flask APIs.
const $ = (s) => document.querySelector(s);
const fmt = (v, suf = "") => (v === null || v === undefined ? "-" : v + suf);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const rs = (v) => "Rs. " + Number(v || 0).toLocaleString("en-IN");
async function api(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}
function card(label, value, color) {
  return `<div class="card k" style="border-left-color:${color || "var(--ind)"}"><div class="l">${label}</div><div class="v">${value}</div></div>`;
}
function bars(el, rows, key) {
  el.innerHTML = rows.map(r => {
    const v = r[key]; const w = v === null ? 0 : Math.min(100, v);
    const c = key === "attendance_pct" ? "#2563eb" : (v !== null && v < 40 ? "var(--red)" : "var(--grn)");
    return `<div class="r"><span><b>${esc(r.code)}</b></span><div class="bar"><i style="width:${w}%;background:${c}"></i></div><span>${fmt(v, "%")}</span></div>`;
  }).join("") || `<p class="mut">No data yet.</p>`;
}
function courseRows(rows) {
  return rows.map(c => `<tr><td><b>${esc(c.code)}</b></td><td>${esc(c.name)}</td>
    <td class="num">${c.enrolled ?? ""}</td><td class="num">${fmt(c.avg_marks, "%")}</td>
    <td class="num">${fmt(c.pass_pct, "%")}</td><td class="num">${fmt(c.attendance_pct, "%")}</td></tr>`).join("");
}
async function adminDashboard() {
  const [s, rows] = await Promise.all([api("/api/summary"), api("/api/analytics/courses")]);
  $("#cards").innerHTML = card("Students", s.students) + card("Courses", s.courses, "#0284c7") +
    card("Fee collected", rs(s.fee_paid), "var(--grn)") + card("Fee due", rs(s.fee_due), "var(--red)") +
    card("Average marks", fmt(s.avg_marks, "%"), "#7c3aed") + card("Pass percentage", fmt(s.pass_pct, "%"), "var(--grn)") +
    card("Attendance", fmt(s.attendance_pct, "%"), "#2563eb");
  $("#coursebody").innerHTML = courseRows(rows);
}
async function studentDashboard() {
  const d = await api("/api/me");
  $("#cards").innerHTML = card("Average marks", fmt(d.overall.avg_marks, "%"), "#7c3aed") +
    card("Attendance", fmt(d.overall.attendance_pct, "%"), "#2563eb") + card("Fee paid", rs(d.fee.paid), "var(--grn)") +
    card("Fee due", rs(d.fee.due), d.fee.due > 0 ? "var(--red)" : "var(--grn)");
  $("#coursebody").innerHTML = d.courses.map(c => `<tr><td><b>${esc(c.code)}</b></td><td>${esc(c.name)}</td>
    <td class="num">${fmt(c.avg_marks, "%")}</td><td class="num">${fmt(c.attendance_pct, "%")}</td></tr>`).join("");
}
async function analytics() {
  const rows = await api("/api/analytics/courses");
  bars($("#b-avg"), rows, "avg_marks"); bars($("#b-pass"), rows, "pass_pct"); bars($("#b-att"), rows, "attendance_pct");
  $("#coursebody").innerHTML = courseRows(rows);
}
document.addEventListener("DOMContentLoaded", () => {
  const p = document.body.dataset.page;
  const run = { "admin-dashboard": adminDashboard, "student-dashboard": studentDashboard, analytics }[p];
  if (run) run().catch(e => { const el = $("#apierr"); if (el) el.textContent = "Could not load data: " + e.message; });
});
