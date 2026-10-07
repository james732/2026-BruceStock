# FinMind 台股分析

此程式會依 `target.txt` 的股票代碼，從 FinMind 取得資料並產生 `analysis.html`，內容包括：

- 最近五個交易日的法人買進、賣出與淨額。
- 最近四個完整 TDCC 週次的收盤、週漲跌與三個大戶持股級距。
- 查詢截止日往前七個日曆日的相關個股新聞。
- 最新收盤價相對於 5 日、10 日、月線（20 日）及季線（60 日）的位置。
- 最近 10 個交易日平均成交量是否大於再前 10 個交易日。
- 各股最近 10 個交易日的總分折線圖與每日分數，包含最新交易日。

總覽會顯示股票代碼與個股名稱。點選任一個股資料列，即可在該列下方展開或收合法人買賣、最近四週股價與持股分級，以及近七日個股新聞。

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

`run_report.bat` 先執行 `uv run main.py`，成功後才執行寄信程式，將 `analysis.html` 與 `analysis_param.html` 一起附在同一封信寄給 `bruce.sy.chen@gmail.com`。任一報告缺少時不寄出。參數版附件請下載後以瀏覽器開啟。`mail_report.py --report` 若指定其他主報告路徑，會一併附上同目錄的 `analysis_param.html`。執行紀錄累加至 `logs\report.log`；成功回傳 0，失敗回傳 1。批次檔會自動切換至專案目錄，並優先使用此電腦已安裝的 uv 路徑。

首次使用或 Gmail 授權失效時，在 PowerShell 執行下列指令完成授權（不會寄信）：

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

## 雲端產生報告與瀏覽網頁（不寄信）

新增的 `Generate stock report` workflow 只執行分析與網站發布，不執行
`run_report.bat` 或 `mail_report.py`，也不需要 Gmail 授權。
平日台北時間 20:00 觸發；GitHub 排程可能延遲，國定假日仍會執行。
可在 Actions 手動執行，選填 `end_date`（YYYY-MM-DD），留空使用台北當日日期。

### 首次設定

1. Settings → Secrets and variables → Actions → Secrets，新增 `FINMIND_TOKEN`。
   使用你目前的 FinMind API token；不要將值貼進程式、commit 或執行紀錄。
2. Settings → Pages → Build and deployment → Source 選 `GitHub Actions`。
   此專案是私有儲存庫，直接啟用 Pages 需帳號方案支援；一般 Pages 網站公開可瀏覽。
   若 Pages 不可用，報告仍可從 Actions artifact 下載；保留程式庫私有。
3. Secrets and variables → Actions → Variables，新增 `PAGES_ENABLED`，值為 `true`。
   未設定時只產生與保存 HTML，略過網站部署。
4. Actions → Generate stock report → Run workflow（選 main）。
   成功後 deploy job 顯示实际網站網址；artifact 保存 30 天。

首頁為生成報告的副本 `index.html`，另保留 `analysis.html` 與
`analysis_param.html`。可由網站網址後加 `analysis_param.html` 開啟參數版。
部署目錄僅含本次生成的 HTML，不含原始碼、token、SQLite 或 Gmail 憑證。
網站目前展示最新一次成功發布的報告，不提供長期歷史頁面。
產生失敗不部署，線上保持上一份成功報告；請查看報告產生時間。
調整 `target.txt` 後，新一次執行即使用新的股票名單。

SQLite 與 TDCC 快取透過 Actions cache 還原與保存。
每次使用新的 cache key 並還原前次版本，避免固定 key 不能更新的問題。
cache 可能被清除，因此仍需可從來源重建，不能當成長期備份。
Actions 執行時間、artifact 與 cache 用量受 GitHub 帳號額度限制。

本機執行也改為從環境變數讀取 token，例如 PowerShell：

```powershell
$env:FINMIND_TOKEN = "<你的 FinMind token>"
uv run main.py
```

原有 Windows 自動寄信排程仍是獨立設定；若你不希望本機繼續寄信，
請停用 `FinMindTrade-WeekdayReport` 工作。雲端 workflow 不會修改本機排程。
