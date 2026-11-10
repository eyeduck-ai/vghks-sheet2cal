"use strict";
const byId = id => document.getElementById(id);
let csrf = "", dirty = false, working = false, lastAttempt, preflightRows;
function notice(message) { const el = byId("notice"); el.textContent = message; el.classList.toggle("visible", Boolean(message)); }
function globalFailure(item) {
  return !item.partial && item.failure_code !== "paused" && Boolean(item.health?.state === "blocked" || item.failure_code || item.consecutive_failures || item.ok === false);
}
function cycleCounts(item) {
  const failed = item.failed_rows ?? (item.row_issues || []).length;
  const counts = `更新 ${item.updated || 0} 筆、刪除 ${item.deleted || 0} 筆`;
  return globalFailure(item) ? `本輪未完成 · ${counts}${failed ? `、需核對 ${failed} 列` : ""}` : `${counts}、需核對 ${failed} 列`;
}
function elapsed(seconds) {
  return seconds >= 86400 ? `${Math.floor(seconds / 86400)} 天` : seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小時` : seconds >= 60 ? `${Math.floor(seconds / 60)} 分鐘` : "不到 1 分鐘";
}
function paintRowIssues(issues = []) {
  byId("row-issues-panel").hidden = issues.length === 0;
  byId("row-issues").replaceChildren(...issues.map(issue => {
    const li = document.createElement("li");
    li.textContent = `${issue.sheet} 第 ${issue.row} 列：${issue.message}`;
    return li;
  }));
}
function sourceLocation(sheet, row) {
  return `${sheet || "分頁不明"}${Number.isInteger(row) && row > 0 ? ` 第 ${row} 列` : ""}`;
}
function sourceFields(fields = []) {
  const labels = {date: "日期", hospital: "院區", mrn: "病歷號", name: "姓名", time: "報到時間", tel: "電話", ga: "麻醉", side: "側別", diagnosis: "診斷", grade: "Grade", procedure: "術式", iol: "IOL", iol_target: "IOL Target", iol_final: "IOL Final", axis: "Axis", plan: "Plan", memo: "心得", condition: "手術內容", cancelled: "取消標記", event_id: "日曆事件綁定", uid: "排程識別", sheet: "分頁", row: "列位置", ScheduleSyncId: "排程識別", CalendarEventId: "日曆事件綁定", tag: "Tag", sn: "SN", cde: "CDE", energy_time: "Energy Time", energy_percent: "Energy %", refraction: "屈光數據", archive_row_type: "封存列類型", custom: "自訂欄", schema: "欄位結構", row_kind: "資料列類型"};
  return [...new Set(fields.map(field => Object.hasOwn(labels, field) ? labels[field] : "其他欄位"))].join("、");
}
function paintSourceChanges(observation, changes = [], error = "") {
  const summary = observation || {};
  const count = key => Number.isInteger(summary[key]) && summary[key] >= 0 ? summary[key] : 0;
  const totals = `新增 ${count("added")}、搬移 ${count("moved")}、修改 ${count("modified")}、移除 ${count("removed")}`;
  const revision = Number.isInteger(summary.revision) ? ` · 來源第 ${summary.revision} 版` : "";
  const diagnosticCount = Object.values(summary.diagnostic_counts || {}).reduce((total, value) => total + (Number.isInteger(value) && value > 0 ? value : 0), 0);
  const diagnostics = diagnosticCount ? ` · 辨識提示 ${diagnosticCount} 項，詳見需核對資料列` : "";
  byId("source-observation").textContent = Number.isInteger(summary.revision) ? `${summary.baseline ? "已建立刀表觀察基準" : "最近刀表觀察"} · ${summary.at || "時間不明"}${revision} · ${totals}${diagnostics}` : "尚未建立刀表觀察基準。";
  byId("source-observation-error").hidden = !error;
  byId("source-observation-error").textContent = error;
  byId("source-empty").hidden = changes.length > 0;
  byId("source-empty").textContent = summary.baseline ? "已建立基準；之後讀到的刀表變動會列在這裡。" : "目前沒有已記錄的刀表變動。";
  byId("source-changes").replaceChildren(...changes.slice(0, 20).map(change => {
    const li = document.createElement("li");
    const label = ({added: "新增", moved: "搬移", modified: "修改", removed: "移除"})[change.kind] || "變動";
    const current = sourceLocation(change.sheet, change.row);
    const previous = sourceLocation(change.previous_sheet, change.previous_row);
    const location = change.kind === "moved" && change.previous_sheet ? `${previous} → ${current}` : current;
    const details = [`${change.at || "時間不明"} · ${label} · ${location}`];
    for (const [key, title] of [["date", "日期"], ["hospital", "院區"]]) {
      const value = change[key], old = change[`previous_${key}`];
      if (change.kind === "removed" && (value || old)) details.push(`${title} ${value || old}`);
      else if (old && old !== value) details.push(`${title} ${old} → ${value || "未填"}`);
      else if (value) details.push(`${title} ${value}`);
    }
    if (change.fields?.length && ["modified", "moved"].includes(change.kind)) details.push(`變更欄位：${sourceFields(change.fields)}`);
    li.textContent = details.join(" · ");
    return li;
  }));
}
async function request(path, values) {
  const response = await fetch(path, values === undefined ? {credentials: "same-origin"} : {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(values)});
  const result = await response.json(); if (!response.ok) throw new Error(result.error || "操作失敗"); return result;
}
function paint(data) {
  lastAttempt = data.last_attempt;
  if (preflightRows && preflightRows.last_attempt !== lastAttempt) preflightRows = undefined;
  const health = data.health || {state: data.partial ? "partial" : globalFailure(data) ? "blocked" : data.last_success ? "ready" : "awaiting_success"};
  const label = ({blocked: "同步受阻", partial: "部分完成，仍有資料待核對", awaiting_success: "等待成功同步", setup_required: "待完成設定", paused: "已暫停", ready: "同步正常"})[health.state] || "等待成功同步";
  const state = `${label} · ${data.enabled ? "定時監控已啟用" : "定時監控停用"} · ${data.running ? "同步執行中 · " : ""}${data.message}`;
  if (byId("state").textContent !== state) byId("state").textContent = state;
  byId("state").classList.toggle("blocked", health.state === "blocked");
  byId("state").classList.toggle("warning", ["partial", "awaiting_success", "setup_required"].includes(health.state));
  byId("last").textContent = data.last_attempt || "尚未執行"; byId("next").textContent = data.enabled ? data.next_check || "即將執行" : "停用";
  const age = health.last_success_age_seconds;
  const ageText = age == null ? "" : health.stale ? `（已 ${elapsed(age)}未成功同步；檢查門檻 ${Math.round(health.success_max_age_seconds / 60)} 分鐘）` : `（距今 ${elapsed(age)}）`;
  byId("success").textContent = (data.last_success || "尚未成功同步") + ageText;
  byId("duration").textContent = data.duration_seconds === undefined ? "—" : data.duration_seconds + " 秒";
  byId("counts").textContent = data.last_attempt ? cycleCounts(data) : "尚未執行";
  byId("failures").textContent = String(data.consecutive_failures || 0);
  byId("scope").textContent = `${data.scope_from} 起（台北時間），過去改期沿用原事件，改到過去更新一次後略過`;
  byId("host").textContent = ({registered: "已登記本機 · 單一主機同步", conflict: "主機登記衝突 · 已停止同步", unregistered: "尚未登記"})[data.host_state] || "待核對";
  byId("email").textContent = data.email || "尚未匯入"; byId("directory").textContent = data.data_dir;
  if (!dirty) { byId("spreadsheet").value = data.spreadsheet_id; byId("calendar").value = data.calendar_id; byId("hours").value = data.hours; byId("fu").checked = data.include_fu; byId("enabled").checked = data.enabled; }
  byId("history").replaceChildren(...data.history.map(item => { const li = document.createElement("li"); li.textContent = `${item.partial ? "部分完成" : item.ok ? "完成" : item.failure_code === "paused" ? "已暫停" : "未完成"} · ${item.message}（${cycleCounts(item)}）`; return li; }));
  paintRowIssues(preflightRows ? preflightRows.issues : data.row_issues);
  byId("source-frequency").textContent = `啟用監控時每 5 分鐘（300 秒）觀察刀表；日曆每 ${Number.isInteger(data.hours) && data.hours > 0 ? data.hours * 60 : 60} 分鐘同步。兩者都不是即時或每次編輯觸發。`;
  paintSourceChanges(data.source_observation, data.source_changes, data.source_observation_error);
  byId("run").disabled = working || data.running || !data.setup_done; byId("prepare").disabled = working || data.running;
}
async function refresh() { try { paint(await request("/api/status")); } catch (error) { notice(error.message); } }
async function action(path, values = {}) { if (working) return; working = true; preflightRows = undefined; try { notice(""); await request(path, values); await refresh(); } catch (error) { notice(error.message); } finally { working = false; await refresh(); } }
byId("settings").addEventListener("input", () => { dirty = true; });
byId("settings").addEventListener("submit", async event => {
  event.preventDefault(); if (!event.currentTarget.reportValidity() || working) return;
  const values = {spreadsheet_id: byId("spreadsheet").value, calendar_id: byId("calendar").value, hours: Number(byId("hours").value), include_fu: byId("fu").checked, enabled: byId("enabled").checked};
  working = true; preflightRows = undefined; try { const file = byId("key").files[0]; if (file) { if (file.size > 1048576) throw new Error("金鑰檔案過大"); values.key = JSON.parse(await file.text()); } await request("/api/config", values); dirty = false; byId("key").value = ""; notice("設定已儲存。"); } catch (error) { notice(error instanceof SyntaxError ? "金鑰不是有效 JSON。" : error.message); } finally { working = false; await refresh(); }
});
byId("run").addEventListener("click", () => action("/api/run")); byId("pause").addEventListener("click", () => { dirty = false; action("/api/pause"); });
byId("prepare").addEventListener("click", () => action("/api/prepare")); byId("remove").addEventListener("click", () => { dirty = false; action("/api/remove-key"); });
(async () => { try { const token = location.hash.slice(1); if (token) { const session = await request("/api/session", {token}); csrf = session.csrf; sessionStorage.setItem("calendarCsrf", csrf); history.replaceState(null, "", location.pathname); } else csrf = sessionStorage.getItem("calendarCsrf") || ""; await refresh(); setInterval(refresh, 5000); } catch (error) { notice(error.message); } })();

byId("check").addEventListener("click", async () => { if (working) return; working = true; try { const result = await request("/api/check", {}); await refresh(); preflightRows = {last_attempt: lastAttempt, issues: result.row_issues || []}; paintRowIssues(preflightRows.issues); notice(result.owner_registered && !result.owner_matches ? "刀表已由另一個監控實例或主機登記，本機不可同步。" : result.ok ? `唯讀檢查完成：月表 ${result.monthly_rows} 列、FU ${result.fu_rows} 列；待準備技術操作 ${result.planned_technical_requests} 筆。` : result.row_issues?.length && !(result.issues || []).length ? `唯讀檢查發現 ${result.row_issues.length} 列需核對；其他正常資料列仍可同步，本輪將暫停刪除日曆事件。尚未寫入雲端。` : "刀表有需核對的格式或識別問題；尚未寫入雲端。"); } catch (error) { notice(error.message); } finally { working = false; } });
