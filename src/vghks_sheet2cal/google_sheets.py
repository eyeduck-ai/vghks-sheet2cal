"""Standalone typed Sheets transport; no clinical account store or hospital SDK."""
import re
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError, TransportError
from google.oauth2 import service_account

from .google_http import GoogleHTTP, GoogleServiceError, response_kind


class GoogleSettings:
    @staticmethod
    def validate_key(value):
        if not isinstance(value, dict) or value.get("type") != "service_account":
            raise ValueError("請匯入服務帳戶 JSON 金鑰。")
        try:
            service_account.Credentials.from_service_account_info(value)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("服務帳戶金鑰內容無法辨識。") from exc

class SheetError(GoogleServiceError):
    pass

class SheetUncertain(SheetError):
    def __init__(self, message, *, code="uncertain", retryable=True):
        super().__init__(message, code=code, retryable=retryable)

class SheetUnavailable(SheetError):
    """A temporary transport/service failure; local intents remain retryable."""

    def __init__(self, message, *, code="transport", retryable=True):
        super().__init__(message, code=code, retryable=retryable)

def spreadsheet_id(value):
    if not isinstance(value, str):
        raise ValueError("刀表網址不正確。")
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    result = match.group(1) if match else value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,180}", result):
        raise ValueError("請輸入 Google 試算表網址或代碼。")
    return result

class SheetsClient:
    def __init__(self, key, session):
        self.key, self.session = key, session
        self.http = GoogleHTTP(session)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(key, safe="")

    def close(self):
        self.session.close()

    def request(self, method, suffix="", **kwargs):
        try:
            response = self.http.request(method, self.base + suffix, **kwargs)
        except requests.RequestException as exc:
            if method == "POST":
                raise SheetUncertain("寫入回應中斷，請按「核對更新結果」；請勿另建相同更新。") from exc
            raise SheetUnavailable("目前無法連線 Google Sheets；本機資料與待處理操作保留，恢復連線後請重新預覽。") from exc
        except TransportError as exc:
            if method == "POST":
                raise SheetUncertain("Google 連線中斷，寫入結果尚未確認；恢復連線後請先核對原批次。") from exc
            raise SheetUnavailable("目前無法連線 Google 授權服務；請確認此網路可連線 Google，稍後重新連線。") from exc
        except GoogleAuthError as exc:
            raise SheetError("服務帳戶授權失敗，請檢查金鑰、系統時間與網路。", code="permission") from exc
        kind = response_kind(response)
        if kind == "rate_limit":
            error = SheetUncertain if method == "POST" else SheetUnavailable
            raise error("Google Sheets 流量限制；稍後重新核對原批次與連線，不需重新分享權限。", code=kind, retryable=True)
        if kind == "permission":
            raise SheetError("Google 授權失敗：確認 Sheets API 已啟用，且服務帳戶有刀表編輯權限。", code=kind)
        if response.status_code == 404:
            raise SheetError("找不到刀表，請確認網址與服務帳戶分享權限。")
        if response.status_code >= 500 or response.status_code == 429:
            if method == "POST":
                raise SheetUncertain("Google 尚未確認寫入結果，請先核對更新結果。", code="transient", retryable=True)
            raise SheetUnavailable("Google 暫時無法完成請求；待處理操作保留，稍後可重新連線。", code="transient", retryable=True)
        if not response.ok:
            raise SheetError("Google 拒絕此更新；請重新讀取刀表、檢查欄位或保護範圍。")
        try:
            return response.json()
        except ValueError as exc:
            if method == "POST":
                raise SheetUncertain("無法辨識寫入回應，請核對更新結果。") from exc
            raise SheetError("無法辨識 Google 回應。") from exc

    def connection_info(self):
        meta = self.request("GET", params={"fields": "spreadsheetId,properties(title),sheets(properties(title))"})
        return {"ok": True, "title": meta.get("properties", {}).get("title", ""),
                "months": [s["properties"]["title"] for s in meta.get("sheets", [])
                           if re.fullmatch(r"\d{6}", s["properties"]["title"])],
                "message": "讀取連線成功；寫入仍取決於編輯者權限與儲存格保護設定。"}

    def read_metadata(self):
        return self.request("GET", params={"fields": "developerMetadata"})

    def read(self):
        return self._read(managed=False)

    def read_all(self):
        """Read every schedule and shared-state tab before reconciliation decisions."""
        return self._read(managed=True)

    def read_for_monitor(self):
        """Full identity coverage, with only Calendar-relevant cell presentation."""
        return self._read(managed=True, monitor=True)

    def read_for_archive(self):
        """Include other tabs to reject formulas that would break on source removal."""
        return self._read(managed=True, all_tabs=True)

    def _read(self, *, managed, all_tabs=False, monitor=False):
        meta = self.request("GET", params={"fields": "spreadsheetId,properties,sheets(properties,protectedRanges,charts,conditionalFormats),developerMetadata,namedRanges"})
        sheets = [s for s in meta.get("sheets", []) if all_tabs or
                  re.fullmatch(r"\d{6}", s["properties"]["title"]) or managed and
                  (s["properties"]["title"] in {"FU", "All", "__VGHKS_SYNC_STATE"}
                   or re.fullmatch(r"刀表封存_\d{4}", s["properties"]["title"]))]
        if monitor:
            self._monitor_grid(sheets)
            return {"properties": meta.get("properties", {}), "sheets": sheets,
                    "all_sheet_properties": [s["properties"] for s in meta.get("sheets", [])],
                    "developerMetadata": meta.get("developerMetadata", []), "namedRanges": meta.get("namedRanges", [])}
        for sheet in sheets:
            p = sheet["properties"]
            if "gridProperties" not in p:
                sheet["rows"] = []
                sheet["merges"] = []
                sheet["column_widths"] = []
                sheet["row_metadata"] = []
                continue
            columns = p["gridProperties"]["columnCount"]
            if columns > 1000:
                raise SheetError("刀表分頁超過 1,000 欄，請確認選擇的是手術刀表。")
            last = column_name(columns - 1)
            row_count = p["gridProperties"]["rowCount"]
            data = []
            title = p["title"].replace("'", "''")
            dimensions = "columnMetadata(pixelSize,hiddenByUser),rowMetadata(pixelSize,hiddenByUser)"
            cell_fields = "userEnteredValue,effectiveValue,formattedValue,userEnteredFormat,dataValidation,note,textFormatRuns"
            if all_tabs:
                cell_fields += ",effectiveFormat"
            elif sheet.get("conditionalFormats"):
                # Reports need the displayed cancellation line without loading
                # every effective font/border on routine hourly scans.
                cell_fields += ",effectiveFormat(textFormat(strikethrough))"
            # Bounded reads preserve typed values, formulas, validation and formatting.
            for start in range(1, row_count + 1, 400):
                result = self.request("GET", params=[
                    ("ranges", f"'{title}'!A{start}:{last}{min(start + 399, row_count)}"),
                    ("fields", f"sheets(data(startRow,startColumn,{dimensions},rowData(values({cell_fields}))),merges)")])
                part = (result.get("sheets") or [{}])[0]
                data.extend(part.get("data", []))
                sheet["merges"] = part.get("merges", [])
            rows = []
            for block in data:
                for index, row in enumerate(block.get("rowData", []), block.get("startRow", 0)):
                    while len(rows) <= index:
                        rows.append([])
                    rows[index] = row.get("values", [])
            sheet["rows"] = rows
            sheet["column_widths"] = next((b["columnMetadata"] for b in data if b.get("columnMetadata")), [])
            metadata = []
            for block in data:
                start = block.get("startRow", 0)
                values = block.get("rowMetadata", [])
                while len(metadata) < start + len(values):
                    metadata.append({})
                metadata[start:start + len(values)] = values
            sheet["row_metadata"] = metadata
        return {"properties": meta.get("properties", {}), "sheets": sheets,
                "all_sheet_properties": [s["properties"] for s in meta.get("sheets", [])],
                "developerMetadata": meta.get("developerMetadata", []),
                "namedRanges": meta.get("namedRanges", [])}

    def _monitor_grid(self, sheets):
        """Batch bounded ranges across tabs; retain notes/runs for safe ID setup.

        Native write/archive readers remain separate. Technical setup only
        writes narrow values or uses native column moves, preserving other styles.
        """
        pending, indexed = [], {sheet["properties"]["sheetId"]: sheet for sheet in sheets}
        blocks = {sid: [] for sid in indexed}
        for sheet in sheets:
            p = sheet["properties"]
            sheet.update(rows=[], merges=[], column_widths=[], row_metadata=[])
            grid = p.get("gridProperties", {})
            columns = grid.get("columnCount", 0)
            if columns > 1000:
                raise SheetError("刀表分頁超過 1,000 欄，請確認選擇的是手術刀表。")
            if not columns:
                continue
            title, last = p["title"].replace("'", "''"), column_name(columns - 1)
            for start in range(1, grid["rowCount"] + 1, 400):
                end = min(start + 399, grid["rowCount"])
                pending.append((f"'{title}'!A{start}:{last}{end}", (end - start + 1) * columns))
        # A strikethrough may come from user format, rich text or a rule.
        values = "userEnteredValue,effectiveValue,formattedValue,note,textFormatRuns,userEnteredFormat(textFormat(strikethrough)),effectiveFormat(textFormat(strikethrough))"
        fields = f"sheets(properties(sheetId),merges,data(startRow,startColumn,columnMetadata(pixelSize,hiddenByUser),rowMetadata(pixelSize,hiddenByUser),rowData(values({values}))))"
        while pending:
            ranges, cells = [], 0
            while pending and len(ranges) < 4:
                if ranges and cells + pending[0][1] > 40000:
                    break
                selected, count = pending.pop(0)
                ranges.append(("ranges", selected))
                cells += count
            result = self.request("GET", params=[*ranges, ("fields", fields)])
            for part in result.get("sheets", []):
                sid = part.get("properties", {}).get("sheetId", 0)
                if sid not in indexed:
                    raise SheetError("Google 回傳未預期的分頁，停止本輪核對。")
                blocks[sid].extend(part.get("data", []))
                indexed[sid]["merges"] = part.get("merges", [])
        for sid, data in blocks.items():
            sheet = indexed[sid]
            for block in data:
                start = block.get("startRow", 0)
                for index, row in enumerate(block.get("rowData", []), start):
                    while len(sheet["rows"]) <= index:
                        sheet["rows"].append([])
                    sheet["rows"][index] = row.get("values", [])
                if block.get("columnMetadata"):
                    sheet["column_widths"] = block["columnMetadata"]
                dimensions = block.get("rowMetadata", [])
                while len(sheet["row_metadata"]) < start + len(dimensions):
                    sheet["row_metadata"].append({})
                sheet["row_metadata"][start:start + len(dimensions)] = dimensions

    def write(self, requests_):
        from .sheet_archive_lock import assert_archive_available

        if requests_:
            assert_archive_available(self.read_metadata(), getattr(self, "archive_owner", ""))
        return self.request("POST", ":batchUpdate", json={"requests": requests_, "includeSpreadsheetInResponse": False})

def column_name(index):
    value = ""
    while index >= 0:
        index, rem = divmod(index, 26)
        value = chr(65 + rem) + value
        index -= 1
    return value
