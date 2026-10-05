# FinMind 台股分析

此程式會依 `target.txt` 的股票代碼，從 FinMind 取得資料並產生 `analysis.html`，內容包括：

- 最近三個交易日的法人買進、賣出與淨額。
- 最近四個完整 TDCC 週次的收盤、週漲跌與三個大戶持股級距。
- 查詢截止日往前七個日曆日的相關個股新聞。
- 最新收盤價相對於 5 日、10 日、月線（20 日）及季線（60 日）的位置。
- 最近 10 個交易日平均成交量是否大於再前 10 個交易日。

總覽會顯示股票代碼與個股名稱。點選任一個股資料列，即可在該列下方展開或收合法人買賣、最近四週股價與持股分級，以及近七日個股新聞。

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

## 計算口徑

- 均線採交易日收盤價的簡單平均，並包含最新交易日。
- 月線為 20 個交易日，季線為 60 個交易日。
- 成交量比較採兩段不重疊的 10 個交易日。
- 法人淨額為買進股數減賣出股數，報告換算成張（1 張＝1,000 股）。
- 所有張數與股數均以整數顯示，並採傳統四捨五入。
- 自營商淨額合併 `Dealer_self` 與 `Dealer_Hedging`；`Foreign_Dealer_Self` 獨立列示。
- FinMind 新聞 API 每次只提供一天資料，因此程式會逐日查詢七次，並依連結去除重複新聞。
- 最近四週持股比例取自 TDCC 開放資料；歷史快照會存放於 `tdcc_cache`，最新週會與 TDCC 官方 OpenAPI 交叉核對。
- 顯示四週漲跌需要五個週次的收盤價；若集保資料日休市，採該日以前最近一個交易日的收盤價。
- `＞400張～≤800張` 合併 TDCC 分級 12、13；`＞800張～≤1千張` 使用分級 14；`＞1千張` 使用分級 15，並以分級 17 的集保庫存總股數重新計算比例。

API token 依目前專案需求直接寫在 `main.py`。請勿公開分享此檔案或將其提交到公開儲存庫。

## 平日 20:00 自動寄送

`run_report.bat` 先執行 `uv run main.py`，成功後才執行寄信程式，將 `analysis.html` 附件寄給 `Bruce1_Chen@asus.com`。執行紀錄累加至 `logs\report.log`；成功回傳 0，失敗回傳 1。批次檔會自動切換至專案目錄，並優先使用此電腦已安裝的 uv 路徑。

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
