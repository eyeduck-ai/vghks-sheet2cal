# VGHKS Sheet2Cal

獨立的 Google 刀表 → Google Calendar 常駐監控，包含月份手術與 FU／All，預設每 **60 分鐘**同步。部署於 Linux，不需要院內 SDK、院內帳號或主程式持續開啟。

保留永久 `ScheduleSyncId`、既有 `CalendarEventId`、固定新事件 ID、跨月搬列、取消灰色事件、原生封存保護、寫後讀回核對及連續缺列刪除確認。同步結果不回寫院內系統。監控會維護刀表技術欄與核對後的院區，實際日曆事件由單一指定主機寫入。

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

正式來源位於 [op_spreadsheet 的刀表契約](https://github.com/eyeduck-ai/op_spreadsheet/tree/codex-shared-sheet-contract/contracts)，Python 共用套件位於同 repo 的 `packages/python`。本專案依賴固定的完整 Git commit；`compatibility.json` 記錄已驗證的搭配版本。一般更新不會在背景下載規格。

主程式與此監控使用同一份 `vghks-sheet-core` 欄位解析、永久識別、共享基準與日曆呈現。GAS 使用生成的欄位定義，並以真實 GAS 匯出案例驗證解析及操作語意。更新步驟見該 repo 的 `docs/SHEET_CONTRACT.md`。

## 開發驗證

```bash
uv sync --locked --extra dev
uv run --locked --extra dev ruff check src tests scripts
uv run --locked --extra dev python -m unittest discover -s tests -v
uv run --locked python -m vghks_sheet_core.verify
```

測試只使用合成資料與記憶體 Google 用戶端，涵蓋 FU、改期、事件 ID、重啟續接、刪除確認、API 重試、封存互斥、跨程序鎖、loopback 驗證與 SIGTERM 停止。CI 另取得固定 GAS source，執行真實匯出器，核對共用核心及指定主程式版本。

`UPSTREAM.json` 記錄拆分來源。原 `vghks-bot` 的 Windows 監控入口暫留作相容用途；同一刀表仍僅啟用一個監控。
