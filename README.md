# VGHKS Sheet2Cal

獨立的 Google 刀表 → Google Calendar 常駐監控，包含月份手術與 FU／All，預設每 **60 分鐘**同步。部署於 Linux，不需要院內 SDK、院內帳號或主程式持續開啟。

保留永久 `ScheduleSyncId`、既有 `CalendarEventId`、固定新事件 ID、跨月搬列、取消灰色事件、原生封存保護、寫後讀回核對及連續缺列刪除確認。同步結果不回寫院內系統。監控只維護必要系統識別與事件綁定。患者醫院欄可留白、手填或保留公式，不補填或覆寫；日期與院區依最近有效刀日判斷。Calendar 由單一指定主機寫入。

## 同步範圍

日期依 **Asia/Taipei** 判斷，今天包含整天。月份手術與 FU／All 共用下列規則：

| 排程狀態 | 日曆處理 |
| --- | --- |
| 目前在今天或未來，包含從過去改回 | 同步原事件，沿用永久 ID。 |
| 原已同步的今天／未來排程改到過去 | 更新原事件一次，核對並保存完成狀態後略過。 |
| 已確認完成且仍在過去，或日期自然成為過去 | 不再查詢或變更日曆。 |

改期沿用 ScheduleSyncId 與 CalendarEventId，不需新增列；失敗或重啟先續接待核對操作。原事件不存在時不補建過去事件。刀日與無日期 FU 不建事件；患者手填院區不改變刀日的有效院區。

刀表仍完整讀取受支援月份、FU／All 與封存的排程 ID，以保護跨月搬列、封存及刪除確認；不依月份名稱略過分頁。缺列且已過去或日期不明的事件保留。

| 日常操作 | 識別與事件 |
| --- | --- |
| 手動輸入新排程 | 自動配置兩個隱藏 ID，不需手填。 |
| 從左側列號拖移整列到其他刀日 | 兩個 ID 隨列移動，更新原事件；日期及院區依目的刀日。 |
| 整列刪除，在另一列重新輸入 | 視為新排程，即使同病人也不自動合併；舊的今天／未來事件經兩輪完整成功確認後刪除，過去事件保留。 |

可見儲存格搬移若留下孤立 ID，系統保留相關事件，暫停可能相關的新列；其他可信列仍可同步。不依姓名或病歷號推測搬移關係。

## 來源觀察與變更紀錄

已啟用時，每 **5 分鐘唯讀觀察刀表**，日曆仍每 **60 分鐘**同步。每輪同步在配置 ID 前及寫後也保存來源版本；觀察本身不配置 ID、不操作 Calendar、不累計缺列刪除確認。

本機 SQLite 保存最近 200 次有變動的來源版本及最多 10,000 筆列變更；未變動讀取不重複累積。網頁顯示最近 20 筆分頁、列號、日期、院區及變更欄名，不回顯病歷號或姓名。私有前後內容不加入 Git。

這是完整讀取之間的淨變化，不保證捕捉快速連續編輯的每個中間狀態。來源觀察與已確認日曆結果分開顯示；新增、改期及刪除以最新可信來源決定。

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

`check` 唯讀檢查 Google 連線、刀表格式與識別準備計畫。`prepare` 會維護必要技術欄、核對既有綁定並登記此主機，完成後仍停用；`enable` 才開始同步。每份刀表只登記一台主機，其他主機不可覆寫登記。請保留既有兩個 ID，不用姓名或列號推測綁定。

也可執行 `.runtime/service-venv/bin/vghks-sheet2cal open-ui`，在本機狀態頁匯入 JSON、檢查、準備及啟用。入口 token 每次服務啟動都會更換。

病歷號可在改院區時暫空，再填新院區病歷號；已綁定排程只要非空病歷號或姓名仍一致，就沿用原 `ScheduleSyncId` 與 `CalendarEventId`。日曆呈現刀表目前的值，不回填舊病歷號。姓名與病歷號都變成另一人、事件綁定不符、單列日期或事件資料有誤時，只保留該列待核對，其餘列繼續同步。首次綁定仍核對主程式病歷號基準。

狀態頁會顯示「部分完成」與問題分頁、列號。重複或孤立 ID 會隔離所有相連列；不選第一列作為正確資料，不清除來源值，其他可信列繼續同步。有已知未解問題時，整輪不刪除事件並重設確認次數，恢復後須連續兩輪完整成功才可刪除。必要表頭或刀日結構不明、既有綁定分頁遺失系統欄標題、主機衝突及連線／授權問題仍停止整輪。

待同步目標與已確認 Calendar 現況分開保存。未確認操作的來源列消失時，先查實際事件，再決定保留過去事件或確認刪除未來事件。更新及刪除採 Calendar 版本條件；事件在核對後被修改則保留並重新核對，不覆蓋競爭寫入。

技術欄初始化使用伺服器端原值條件，不覆寫其他寫入者剛填入的 ID。回寫 CalendarEventId 時使用短暫的原生列 metadata 錨點，寫前核對 UID、患者與欄位，整列拖移後仍定位同一列；完成後移除自己的錨點，失敗或重啟會核對並清理。它不保證任意儲存格貼上、直接更改隱藏 ID 或同時重排欄位的跨欄原子性，寫後讀回仍保留。

## Google API 用量與人工編輯輔助

監控直接使用 Sheets API、Calendar API。網頁每五秒更新只讀本機狀態；每五分鐘來源觀察只讀 Sheets，不寫值、不呼叫 Calendar。GAS 負責人工編輯格式、手打時間整理及明確選單操作；監控只自動維護技術欄與事件綁定，不補患者院區、病歷號、姓名、報到時間或 GA。API 寫入不觸發 GAS 編輯觸發器，因此不形成互相回寫循環。

每次要求前先將用量預留到本機 SQLite，包含觀察、連線檢查、準備、手動同步、失敗與重試；不同 client session 與重啟共用同一專案的計數。限制如下：

| API | 最近 60 秒 | 最近 24 小時 |
| --- | --- | --- |
| Sheets | 讀寫合計 45 次 | 10,000 次 |
| Calendar | 120 用量單位 | 10,000 用量單位 |

Calendar PATCH 計三單位，其餘要求計一單位。分鐘滿額會分段等待，可暫停；24 小時滿額停止送出並保留待核對狀態，額度釋出後重試。狀態頁顯示本監控的用量；尚未部署用量保護前的呼叫不會回填計數。儲存失敗時不發出 API 要求。

以 35 筆未來排程、一次完整來源讀取五個要求估算，無變動時每日約 1,560–1,680 次 Sheets 要求及 840 次 Calendar GET。大量新增或更新會增加要求，但由上述上限節流；已確認的過去事件不做逐筆 Calendar 查詢。

截至 2026-11-10，官方 [Sheets 標準配額](https://developers.google.com/workspace/sheets/api/limits) 為每使用者每分鐘讀／寫各 60 次、每專案各 300 次；[Calendar 標準配額](https://developers.google.com/workspace/calendar/api/guides/quota) 對新專案為每使用者每分鐘 600 次、每專案 10,000 次，標準每日計費門檻為每專案 1,000,000 次。既有專案可有不同額度，Google 已公告後續超標計費規劃。這是本監控的保守上限，不是整個 Cloud 專案帳單保證；其他程式仍共用 Google 配額。監控不申請提高配額或啟用計費。

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

`/healthz` 表示服務存活；`/readyz` 只有在已設定、啟用、最近兩個週期內有完整成功同步，且目前沒有整輪失敗或部分完成問題時回覆 200。整輪失敗立即標記 `blocked`；狀態頁另顯示最近成功距今時間。無設定時健康狀態為 `setup_required`。日誌省略憑證、入口 token 與病人資料，詳細問題顯示在已驗證的本機狀態頁。

更新程式後再次執行 `bash scripts/deploy.sh`。更新不重建狀態資料庫；備份及搬移請保留完整狀態目錄，並在複製前停止服務。尚未完成的操作不能僅從刀表重建。

## 單一主機防護

刀表用固定唯一的 DOCUMENT metadata ID 登記監控識別、刀表、日曆與本機指紋；同時登記只會有一台成功。指紋由 Linux machine-id 與使用者識別雜湊產生，雲端不保存原始 machine-id。複製資料庫到另一台電腦也無法沿用登記。

每輪同步及每次寫入前核對登記；不符合時停用同步並在網頁顯示衝突。同機另有資料目錄鎖與刀表跨程序鎖。登記不因離線或逾時自動轉移，重新啟動同一服務可直接恢復。更換實體主機屬管理員維護，須先停止所有監控、備份並核對登記後重新配置。

## 共用規格與相容性

契約版本 **1.3.0**。正式來源位於 [op_spreadsheet 的刀表契約](https://github.com/eyeduck-ai/op_spreadsheet/tree/master/contracts)，Python 共用套件位於同 repo 的 `packages/python`。本專案依賴固定的完整 Git commit；`compatibility.json` 記錄已驗證的搭配版本。一般更新不會在背景下載規格。

主程式與此監控使用同一份 `vghks-sheet-core` 欄位解析、永久識別、共享基準與日曆呈現。GAS 使用生成的欄位定義，並以真實 GAS 匯出案例驗證解析及操作語意。更新步驟見該 repo 的 `docs/SHEET_CONTRACT.md`。

三個產品可各自更新與發布；只有共用欄位、識別或狀態語意改變時才需協調契約更新。`compatibility.json` 的主程式版本是 CI 驗證基準，不是執行時依賴。本版已驗證主程式 6.19.1、GAS 2026.10.27 與共用核心 1.3.0，監控不載入主程式或院內 SDK。

監控處理規則另以 `calendar-monitor-policy.json` **1.4.0** 固定版本。它沿用資料規格 1.3.0，定義監控的局部隔離、來源觀察與已確認狀態；嚴格共用解析器仍回報原始衝突，主程式與 GAS 的寫入驗證不受影響。CI 分別核對固定的核心與監控規則來源，執行時不下載最新版。

## 開發驗證

```bash
uv sync --locked --extra dev
uv run --locked --extra dev ruff check src tests scripts
uv run --locked --extra dev python -m unittest discover -s tests -v
uv run --locked python -m vghks_sheet_core.verify
```

測試只使用合成資料與記憶體 Google 用戶端，涵蓋 FU、改期、事件 ID、重啟續接、刪除確認、API 重試、封存互斥、跨程序鎖、loopback 驗證與 SIGTERM 停止。CI 另取得固定 GAS source，執行真實匯出器，核對共用核心及指定主程式版本，並以實際主程式寫入器驗證監控的排程生命週期。

`UPSTREAM.json` 記錄拆分來源。主程式 repo 只提供 VGHKS-bot.exe；監控在此 repo 獨立維護、發布及部署。每份刀表只允許一台主機同步。

## 獨立發布

[Releases](https://github.com/eyeduck-ai/vghks-sheet2cal/releases) 提供 Python wheel、完整 source distribution 及 SHA256SUMS.txt。常駐部署使用本 repo 的 scripts/deploy.sh，版本與主程式分開維護。

更新 pyproject、套件版本與 CHANGELOG，確認相容 commit 及 CI 成功後推送相符的 v* 標籤；Release workflow 再執行完整驗證並建立安裝套件。來源與發行包不包含本機設定、金鑰、資料庫或備份。
