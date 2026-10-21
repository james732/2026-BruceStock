# FinMind 台股分析

此程式會依 `target.txt` 的股票代碼，從 FinMind 取得資料並產生 `analysis.html`，內容包括：

- 最近五個交易日的法人買進、賣出與淨額。
- 最近四個完整 TDCC 週次的收盤、週漲跌與三個大戶持股級距。
- 查詢截止日往前七個日曆日的相關個股新聞。
- 最新收盤價相對於 5 日、10 日、月線（20 日）及季線（60 日）的位置。
- 最近 10 個交易日平均成交量是否大於再前 10 個交易日。
- 各股最近 10 個交易日的總分折線圖與每日分數，包含最新交易日。

總覽會顯示股票代碼與個股名稱。點選任一個股資料列，即可在該列下方展開或收合法人買賣、最近四週股價與持股分級，以及近三日個股新聞。

執行時也會在報表同一目錄產生 `analysis_param.html`。用瀏覽器開啟後，可調整六項評分參數（正數加分、負數扣分、0 不計分，每項限 -100 至 100 的整數），分數、顏色與目前排序會即時更新。起始分與上限皆為 100 分；可按「恢復預設」還原，重新整理也會回到預設值。參數調整只影響這個頁面。

展開個股後，最上方顯示 10 日總分圖表及日期／分數表。兩版皆採內嵌 SVG，下載 HTML 後可離線查看；參數版修改或恢復預設時，會同步重算整條曲線。圖表由舊到新排列，橫軸為交易日期，縱軸預設 0–100，出現負分時自動向下延伸；窄螢幕可橫向捲動。

## 執行方式

```powershell
uv run main.py
```

指定查詢截止日或輸出檔名：

```powershell
uv run main.py --end-date 2026-10-02 --output analysis.html
```

執行離線單元測試：

```powershell
uv run python -m unittest -v
```

若有 Node.js，單元測試也會執行報表 JavaScript 的模擬 DOM 測試（不需瀏覽器或 npm 套件）；無 Node.js 時只跳過該項。

## SQLite 快取

第一次執行會自動建立專案目錄下的 `data/finmind.sqlite3`，保存原始資料、成功查詢日期與更新時間；後續先查快取，再向 FinMind 補齊。資料庫不含 API token，也不存評分結果，調整參數不需重新下載。

- 股價及法人：沿用 180 個日曆日的查詢範圍，只下載缺漏區間及截至查詢截止日最近 7 個日曆日，接收近期補登與更正。每個資料集會合併連續日期為一次請求。
- 新聞：逐日快取；當日每次刷新，其餘日期有效期 24 小時，僅處理報表所需的 7 天。
- 股票名稱：整批快取 7 天，過期或缺少目標股票時重新取得。
- 已成功查詢但沒有資料的日期也會記錄，避免將休市日誤判為缺漏；近期仍會刷新。
- TDCC 維持既有 `tdcc_cache`。

手動刷新本次報表需要的所有 FinMind 資料（保留其他歷史快取）：

```powershell
uv run main.py --refresh-cache
```

API 失敗不會覆蓋成功快取；若快取完整，報表會使用快取並標示更新失敗與最後成功時間。必要的股價或法人區間缺漏且下載失敗時，停止產生新報表。新聞失敗會在報表標示資料不完整；名稱失敗則使用快取名稱或股票代碼。SQLite 無法讀寫時改為直接下載，不自動刪除既有資料庫。執行紀錄會列出快取命中與補抓區間。

## 計算口徑

- 均線採交易日收盤價的簡單平均，並包含最新交易日。
- 月線為 20 個交易日，季線為 60 個交易日。
- 成交量比較採兩段不重疊的 10 個交易日。
- 每日歷史總分只用截至該日的資料，沿用現有六項評分規則；法人取截至該日最近十個有法人資料的交易日，十日平均淨買超（十日合計淨買超除以 10）為正值，且嚴格大於截至該日近十日平均成交量的 3% 才加 10 分，不足十日不加分。總覽與曲線最後一點採同一份分數及評分條件。
- 以每檔股票最近 10 個有行情的交易日為範圍；早期日期不足 60 筆行情時標示「資料不足」，曲線斷開而不補零。最新日期不足 60 筆仍依既有規則停止報表產生。
- 法人淨額為買進股數減賣出股數，報告換算成張（1 張＝1,000 股）。
- 最近 10 日與前 10 日均量也換算成張顯示；評分使用未四捨五入的原始股數計算。
- 所有張數與股數均以整數顯示，並採傳統四捨五入。
- 自營商淨額合併 `Dealer_self` 與 `Dealer_Hedging`；`Foreign_Dealer_Self` 獨立列示。
- FinMind 新聞 API 每次只提供一天資料，因此程式按日查詢快取缺漏或到期的新聞，並依連結去除重複新聞。
- 最近四週持股比例取自 TDCC 開放資料；歷史快照會存放於 `tdcc_cache`，最新週會與 TDCC 官方 OpenAPI 交叉核對。
- 顯示四週漲跌需要五個週次的收盤價；若集保資料日休市，採該日以前最近一個交易日的收盤價。
- `＞400張～≤800張` 合併 TDCC 分級 12、13；`＞800張～≤1千張` 使用分級 14；`＞1千張` 使用分級 15，並以分級 17 的集保庫存總股數重新計算比例。

API token 由環境變數 `FINMIND_TOKEN` 提供；未設定時會停止產生報告。雲端執行使用同名 Actions Secret。

## 平日 20:00 自動寄送

`mail_report.py` 預設只附上 `analysis_momentum.html`，測試收件人為 `james732@gmail.com`；
不再要求或附上 `analysis_param.html`。內文列出評分v2總分，缺資料另列已知小計。
指定 `--report` 時只附該檔案，指定 `--recipient` 可覆寫收件人。
`run_report.bat` 先產生原版與 v2，成功後只寄出 v2。任一產生步驟失敗時不寄信。

首次使用或 Gmail 授權失效時，在 PowerShell 執行下列指令完成授權（不會寄信）。
即使本機 `gmail_token.json` 的 Refresh Token 已失效，互動模式也會重新開啟 Google 授權頁面，取得新的授權：

```powershell
uv run mail_report.py --authorize-only
```

安裝 Windows 工作排程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_schedule.ps1
```

工作名稱為 `FinMindTrade-WeekdayReport`，週一至週五 20:00 依 Windows 本機時區執行，目前電腦為台北時區。使用目前登入的 Windows 帳號，不儲存密碼；執行時需保持登入，電腦需開機且未休眠。錯過的排程不會補寄，也不會自動重試；前一次尚未結束時不會重複啟動。

若手動建立排程，程式填 `C:\Windows\System32\cmd.exe`，引數填 `/d /c ""C:\Users\3\Desktop\finmindtrade\run_report.bat""`，開始位置填 `C:\Users\3\Desktop\finmindtrade`，觸發條件設每週一至五 20:00。

手動執行 `run_report.bat` 會產生報表並實際寄信。排程採非互動授權模式；需要重新授權時會失敗並記錄錯誤，不會等待瀏覽器登入。

## 雲端每日報告與自動補救

`Generate stock report` 以每日台北時間 **20:18** 為目標，包含週末及假日。
原生 GitHub Actions 排程可能延遲或遺漏，無法保證 20:18 準時啟動或完成。
20:18 啟動後仍需產生、部署及寄送時間。備援喚醒在 20:38、20:58，
21～23 時每小時 18／38／58 分，其他時段每小時 18 分。
每次檢查最近已到期日報，已寄送便跳過，不會每次重新生成或寄信。
程式與離線測試由 `Offline tests` workflow 在 main push 與 PR 執行；push 不再更新正式網站。

### 設定與首次啟用

1. Actions secrets：`FINMIND_TOKEN`、`GMAIL_CLIENT_SECRET_JSON`、`GMAIL_TOKEN_JSON`。
   Gmail token 必須含 refresh token，維持 `gmail.send` 權限即可，不需要讀取郵件。
2. Settings → Pages → Build and deployment → Source 選 `GitHub Actions`。
3. Actions repository variable `PAGES_ENABLED=true`。正式發布未啟用 Pages 時會失敗，
   不會跳過部署而繼續寄信。
4. 合併新 workflow 至 main 後，等下一次排程自動處理。程式起始管理日為 2026-10-18；
   不會自動補寄此日前的報告。`report-state` 分支由第一份正式報告自動建立。
   若分支規則禁止 GITHUB_TOKEN 建立／更新該分支，需允許此 workflow 寫入；
   寫入失敗時會停止，不會繞過寄信紀錄。

### 報告日期、版本與保存

- `report_date`：每日批次日期，以台北 20:18 為切換點。例如 10/19 00:39 補跑仍為 10/18。
- `market_as_of`：最近已收盤交易日，可能與批次日期不同。
- `generated_at`、`data_observed_at`：實際產生與取得資料時間。
  延遲補跑使用當時取得的資料，不宣稱重現原定 20:18 的歷史資訊。
- 每日正式版目前固定 r1，首次產生即凍結 HTML 與 SHA-256，後續部署／寄信重試沿用同一份內容。
  不會因多次喚醒而重新計分。修改股票清單或程式，於下一份尚未產生的日報生效。
- `report_metadata.json` 同時放進網站與 artifact；信件日期和時間讀取 metadata，
  不依寄送當下日期或下載附件的檔案時間推測。

自動補救窗口到下一日 20:18；窗口結束後只處理新一期，不累積補寄多天舊報告。
GitHub run 建立時間作為穩定批次基準，舊 run 跨過窗口後重跑會跳過；應使用新的手動 run。
原始排程事件沒有可靠的預定觸發日期，因此此機制按「最近已到期工作」運作。

`report-state` 分支保存 `delivery.json` 與 `reports/YYYY-MM-DD/r1/` 的凍結報告。
每次變更以 Git commit 和非強制 ref 更新作為持久 checkpoint，保留原狀態並檢查衝突。
分支已存在但內容損壞、權限錯誤或讀取失敗都會停止，不能當成尚未寄送。
請勿刪除或人工覆寫此分支；刪除寄送紀錄會失去去重依據。
這些分數、報告、雜湊、run ID 和 Gmail message ID 依 repository 可見性保存，
不存 Gmail 憑證；收件者以雜湊作為 ledger key。凍結報告長期保存在該分支，需留意儲存空間增長。

### Gmail OAuth 授權失效處理

如果 Actions 在 `Send frozen v2 report with bounded retries` 顯示
`invalid_grant: Token has been expired or revoked`，代表 GitHub Actions Secret
`GMAIL_TOKEN_JSON` 內的 Gmail Refresh Token 已失效。修改 workflow 或重跑失敗 job
無法使已失效的 Google 授權恢復。

請在具備 OAuth client JSON 的本機執行 `uv run mail_report.py --authorize-only`，
登入實際負責發信的 Google 帳號並完成授權。確認本機 `gmail_token.json`
存在且包含新的 `refresh_token` 後，到 GitHub repository
**Settings → Secrets and variables → Actions → Repository secrets**
更新 `GMAIL_TOKEN_JSON`，貼入整份 JSON；不要把該檔 commit、貼入 issue 或聊天。
若原本的 OAuth client 不存在，也要從自己的 Google Cloud 專案重新取得對應的 OAuth client JSON。
`GMAIL_CLIENT_SECRET_JSON` 仍須與所使用的 OAuth client 相符。

從本次修正開始，雲端先檢查 Gmail 授權，再保留寄信額度。
授權失效在送出 Gmail API request 前便中止，不扣同日兩次 Gmail 發送額度，
所以更新 Secret 後，同一有效窗口內的後續排程可以自動恢復寄送。
已經在舊程式中耗盡額度的日期不會自動重置：必須先核對失敗紀錄、
寄件備份與 `report-state/delivery.json`，確認並無任何 Gmail API 成功寄出，
才可人工執行受稽核的額度修復；不得直接刪除 ledger。

若 Google OAuth consent screen 處於 **Testing**，某些權限的 Refresh Token
可能有較短有效期限。應至自己的 Google Cloud 專案確認應用程式發布狀態與授權政策，
並排查使用者撤銷授權、Token 失效等原因。

### 寄信與重試限制

每日排程寄到 `bruce.sy.chen@gmail.com`，只附 `analysis_momentum.html`。
先凍結報告、部署 Pages 並保存部署成功，再寄信。
同一批次／r1／收件者 **最多兩次自動 Gmail 呼叫**，成功保存 message ID 後立即停止。
每次呼叫前先保存嘗試次數；API timeout、程序中斷、寄出後寫回失敗，均算一次。
結果不明時後續喚醒允許再試一次，因此最多可能收到兩封同版本信。
到達兩次上限後顯示略過原因，不再自動重試；這不代表郵件一定成功。
可從失敗 run 與 `delivery.json` 的 `sending`／`attempts` 判斷是否需人工處理。
一般重跑也會遵守同一份 ledger，不再用 `run_attempt == 1` 阻止失敗補救。
Gmail 寄送與 GitHub commit 無法形成同一交易，不能絕對保證一定送達或完全沒有重複。

### 手動操作與預覽

Actions → Generate stock report → Run workflow（main）：

- **未勾選 `send_email`**：只產生預覽 artifact，不部署、不寄信、不更動正式 ledger。
  `end_date` 留空使用 run 建立時的台北日期，也可指定過往日期做歷史查詢。
- **勾選 `send_email`**：正式發布最近已到 20:18 的日報，收件者預設 Bruce。
  `end_date` 應留空，或與最近到期日期一致；不能用此入口發布過往日期覆蓋網站。
  已寄過相同收件者便跳過；需要另寄一位收件者可填 `recipient`。
- 手動寄送同樣最多兩次嘗試，不提供繞過上限或強制改寫 r1 的選項。

artifact 保留 30 天。正式網站僅展示最近成功發布內容，保留 `index.html`、
`analysis.html`、`analysis_param.html`、`analysis_momentum.html` 與 metadata。
產生失敗不部署；既有正式報告保持不變。憑證、原始碼、SQLite 不會放進網站或 artifact。
SQLite／TDCC 快取仍由 Actions cache 還原與保存；cache 可清除，不承擔寄送去重責任。

本機執行也改為從環境變數讀取 token，例如 PowerShell：

```powershell
$env:FINMIND_TOKEN = "<你的 FinMind token>"
uv run main.py
```

原有 Windows 自動寄信排程仍是獨立設定；若你不希望本機繼續寄信，
請停用 `FinMindTrade-WeekdayReport` 工作。雲端 workflow 不會修改本機排程。

## 評分v2新頁面

雲端提供 `analysis_momentum.html`，保留原報告並提供頁首切換；正式排程會寄送 v2 附件。
本機另執行 `uv run momentum_report.py`；參數設定在 `momentum_config.json`。
規則、資料映射、缺資料及重複扣分說明見 [MOMENTUM.md](MOMENTUM.md)。


### 本機日期模式

`momentum_report.py --mode auto` 保留既有行為：過往日期走歷史查詢，
不把現在取得的集保週資料當成歷史當時已知。正式延遲日報明確使用 `--mode daily`，
並揭露實際取得時間；`--mode historical` 可明確要求歷史限制。
`--metadata-output PATH` 可輸出資料日及產生時間供凍結流程使用。
`mail_report.py --metadata public/report_metadata.json` 可驗證附件雜湊並使用固定批次資訊；
獨立執行 mail_report.py 不經 ledger，因此不受雲端兩次上限保護。
