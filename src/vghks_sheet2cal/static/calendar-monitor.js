"use strict";
const byId = id => document.getElementById(id);
let csrf = "", dirty = false, working = false;
function notice(message) { const el = byId("notice"); el.textContent = message; el.classList.toggle("visible", Boolean(message)); }
async function request(path, values) {
  const response = await fetch(path, values === undefined ? {credentials: "same-origin"} : {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(values)});
  const result = await response.json(); if (!response.ok) throw new Error(result.error || "操作失敗"); return result;
}
function paint(data) {
  const state = (data.running ? "同步執行中 · " : data.enabled ? "定時監控已啟用 · " : "定時監控停用 · ") + data.message;
  if (byId("state").textContent !== state) byId("state").textContent = state;
  byId("last").textContent = data.last_attempt || "尚未執行"; byId("next").textContent = data.enabled ? data.next_check || "即將執行" : "停用";
  byId("success").textContent = data.last_success || "尚未成功同步";
  byId("duration").textContent = data.duration_seconds === undefined ? "—" : data.duration_seconds + " 秒";
  byId("failures").textContent = String(data.consecutive_failures || 0);
  byId("scope").textContent = `${data.scope_from} 起（台北時間），依刀表目前日期；過去改期沿用原事件`;
  byId("host").textContent = ({registered: "已登記本機 · 單一主機同步", conflict: "主機登記衝突 · 已停止同步", unregistered: "尚未登記"})[data.host_state] || "待核對";
  byId("email").textContent = data.email || "尚未匯入"; byId("directory").textContent = data.data_dir;
  if (!dirty) { byId("spreadsheet").value = data.spreadsheet_id; byId("calendar").value = data.calendar_id; byId("hours").value = data.hours; byId("fu").checked = data.include_fu; byId("enabled").checked = data.enabled; }
  byId("history").replaceChildren(...data.history.map(item => { const li = document.createElement("li"); li.textContent = `${item.ok ? "完成" : item.failure_code === "paused" ? "已暫停" : "需核對"} · ${item.message}`; return li; }));
  byId("run").disabled = working || data.running || !data.setup_done; byId("prepare").disabled = working || data.running;
}
async function refresh() { try { paint(await request("/api/status")); } catch (error) { notice(error.message); } }
async function action(path, values = {}) { if (working) return; working = true; try { notice(""); await request(path, values); await refresh(); } catch (error) { notice(error.message); } finally { working = false; await refresh(); } }
byId("settings").addEventListener("input", () => { dirty = true; });
byId("settings").addEventListener("submit", async event => {
  event.preventDefault(); if (!event.currentTarget.reportValidity() || working) return;
  const values = {spreadsheet_id: byId("spreadsheet").value, calendar_id: byId("calendar").value, hours: Number(byId("hours").value), include_fu: byId("fu").checked, enabled: byId("enabled").checked};
  working = true; try { const file = byId("key").files[0]; if (file) { if (file.size > 1048576) throw new Error("金鑰檔案過大"); values.key = JSON.parse(await file.text()); } await request("/api/config", values); dirty = false; byId("key").value = ""; notice("設定已儲存。"); } catch (error) { notice(error instanceof SyntaxError ? "金鑰不是有效 JSON。" : error.message); } finally { working = false; await refresh(); }
});
byId("run").addEventListener("click", () => action("/api/run")); byId("pause").addEventListener("click", () => { dirty = false; action("/api/pause"); });
byId("prepare").addEventListener("click", () => action("/api/prepare")); byId("remove").addEventListener("click", () => { dirty = false; action("/api/remove-key"); });
(async () => { try { const token = location.hash.slice(1); if (token) { const session = await request("/api/session", {token}); csrf = session.csrf; sessionStorage.setItem("calendarCsrf", csrf); history.replaceState(null, "", location.pathname); } else csrf = sessionStorage.getItem("calendarCsrf") || ""; await refresh(); setInterval(refresh, 5000); } catch (error) { notice(error.message); } })();

byId("check").addEventListener("click", async () => { if (working) return; working = true; try { const result = await request("/api/check", {}); notice(result.owner_registered && !result.owner_matches ? "刀表已由另一個監控實例或主機登記，本機不可同步。" : result.ok ? `唯讀檢查完成：月表 ${result.monthly_rows} 列、FU ${result.fu_rows} 列；待準備技術操作 ${result.planned_technical_requests} 筆。` : "刀表有需核對的格式或識別問題；尚未寫入雲端。"); } catch (error) { notice(error.message); } finally { working = false; await refresh(); } });
