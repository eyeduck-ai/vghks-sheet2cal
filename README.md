# VGHKS Sheet2Cal

獨立的 Google 刀表 → Google Calendar 常駐監控，包含月份手術與 FU／All，預設每 **60 分鐘**同步。部署於 Linux，不需要院內 SDK、院內帳號或主程式持續開啟。

保留永久 `ScheduleSyncId`、既有 `CalendarEventId`、固定新事件 ID、跨月搬列、取消灰色事件、原生封存保護、寫後讀回核對及連續缺列刪除確認。同步結果不回寫院內系統。監控只維護必要系統識別與事件綁定。患者醫院欄可留白、手填或保留公式，不補填或覆寫；日期與院區依最近有效刀日判斷。Calendar 由單一指定主機寫入。

## 同步範圍

每輪掃描所有受支援的 `YYYYMM` 月份與 FU／All，**包含過去與未來日期**，不是只處理今天以後。刀日標題不建立事件，無日期 FU 不建事件；年度封存的 ID 用於保護既有事件，不由封存列建立新事件。手填患者院區不會改變刀日的有效院區。

## 安裝與常駐部署

需要 Linux、Git、uv、systemd 使用者服務。從此專案根目錄執行：

```bash
bash scripts/deploy.sh
```

腳本安裝隔離的 Python 3.11、依 `uv.lock` 建立非 editable 的執行環境，並啟用 `vghks-sheet2cal.service`。使用者 linger 讓服務在重開機後啟動、登出後繼續執行；若主機政策要求授權，請由管理員執行 `loginctl enable-linger <帳號>`。

程式與狀態分開保存：

| 內容 | 位置 |
| --- | --- |
| 固定依賴及程式 | `.runtime/service-venv/` |
| Python 執行環境 | `.runtime/python/` |
| systemd 單元 | `~/.config/systemd/user/vghks-sheet2cal.service` |
| 設定、金鑰、事件綁定及操作紀錄 | `~/.local/state/vghks-sheet2cal/calendar.sqlite3` |
| 本機狀態頁入口 | 同一狀態目錄內的 `.runtime.json`，僅本機使用者可讀 |

初次啟動會等待設定，定時同步保持停用。服務只聽 `127.0.0.1:8765`，狀態頁採本機入口 token、session 與 CSRF 驗證；不公開至網際網路。金鑰與 SQLite 不加入 Git。憑證包裝金鑰保存在同一資料庫，持有整份資料庫者可使用憑證；請依主機私有資料保管。

## Google 設定與啟用

將刀表以編輯權限分享給服務帳戶，並將目標日曆以「變更活動」權限分享給相同帳戶，啟用 Sheets API 與 Calendar API。

複製 `config.example.json` 到私有的 `.local/config.json`，填入刀表網址、Calendar ID、服務帳戶 JSON 本機路徑；保持 `include_fu: true`、`hours: 1`。接著執行：

```bash
.runtime/service-venv/bin/vghks-sheet2cal configure --file .local/config.json
.runtime/service-venv/bin/vghks-sheet2cal check
.runtime/service-venv/bin/vghks-sheet2cal prepare
.runtime/service-venv/bin/vghks-sheet2cal enable
.runtime/service-venv/bin/vghks-sheet2cal status
```

`check` 唯讀檢查 Google 連線、刀表格式與識別準備計畫。`prepare` 會維護必要技術欄、核對既有綁定並登記此主機，完成後仍停用；`enable` 才開始同步。若已有其他主機登記，先停止舊主機，再明確執行 `prepare --takeover`。請保留既有兩個 ID，不用姓名或列號推測綁定。

也可執行 `.runtime/service-venv/bin/vghks-sheet2cal open-ui`，在本機狀態頁匯入 JSON、檢查、準備及啟用。入口 token 每次服務啟動都會更換。

## 維運

```bash
systemctl --user status vghks-sheet2cal.service
journalctl --user -u vghks-sheet2cal.service -n 40 --no-pager
systemctl --user restart vghks-sheet2cal.service
.runtime/service-venv/bin/vghks-sheet2cal pause
.runtime/service-venv/bin/vghks-sheet2cal enable
.runtime/service-venv/bin/vghks-sheet2cal run-once
```

`run-once` 立即排入單輪同步；稍後用 `status` 或狀態頁查看完成結果。`pause` 會持久保存停用設定；systemd 重啟不會自行解除暫停。停止服務時在操作安全邊界保存狀態，重啟後續接既有事件。

`/healthz` 表示服務存活；`/readyz` 只有在已設定、啟用且最近兩個週期內有成功同步時回覆 200。無設定時健康狀態為 `setup_required`。日誌省略憑證、入口 token 與病人資料，詳細問題顯示在已驗證的本機狀態頁。

更新程式後再次執行 `bash scripts/deploy.sh`。更新不重建狀態資料庫；備份及搬移請保留完整狀態目錄，並在複製前停止服務。尚未完成的操作不能僅從刀表重建。

## 共用規格與相容性

契約版本 **1.1.0**。正式來源位於 [op_spreadsheet 的刀表契約](https://github.com/eyeduck-ai/op_spreadsheet/tree/master/contracts)，Python 共用套件位於同 repo 的 `packages/python`。本專案依賴固定的完整 Git commit；`compatibility.json` 記錄已驗證的搭配版本。一般更新不會在背景下載規格。

主程式與此監控使用同一份 `vghks-sheet-core` 欄位解析、永久識別、共享基準與日曆呈現。GAS 使用生成的欄位定義，並以真實 GAS 匯出案例驗證解析及操作語意。更新步驟見該 repo 的 `docs/SHEET_CONTRACT.md`。

## 開發驗證

```bash
uv sync --locked --extra dev
uv run --locked --extra dev ruff check src tests scripts
uv run --locked --extra dev python -m unittest discover -s tests -v
uv run --locked python -m vghks_sheet_core.verify
```

測試只使用合成資料與記憶體 Google 用戶端，涵蓋 FU、改期、事件 ID、重啟續接、刪除確認、API 重試、封存互斥、跨程序鎖、loopback 驗證與 SIGTERM 停止。CI 另取得固定 GAS source，執行真實匯出器，核對共用核心及指定主程式版本。

`UPSTREAM.json` 記錄拆分來源。主程式 repo 只提供 VGHKS-bot.exe；監控在此 repo 獨立維護、發布及部署。同一刀表只啟用一個監控。

## 獨立發布

[Releases](https://github.com/eyeduck-ai/vghks-sheet2cal/releases) 提供 Python wheel、完整 source distribution 及 SHA256SUMS.txt。常駐部署使用本 repo 的 scripts/deploy.sh，版本與主程式分開維護。

更新 pyproject、套件版本與 CHANGELOG，確認相容 commit 及 CI 成功後推送相符的 v* 標籤；Release workflow 再執行完整驗證並建立安裝套件。來源與發行包不包含本機設定、金鑰、資料庫或備份。
