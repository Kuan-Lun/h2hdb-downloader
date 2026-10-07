# H2HDB Downloader

H2HDB Downloader 讓你的 Python 程式將 E-Hentai／ExHentai 圖庫提交到 H@H 下載，
並將待辦工作保存在 h2hdb 資料庫。你可以用 CSV 加入下載清單、處理需要重新下載的
圖庫，或連同作者與社團的相關作品一起下載。

這是供程式呼叫的函式庫，沒有獨立 CLI 或常駐服務。你的腳本負責決定執行時機；
[HBrowser](https://github.com/Kuan-Lun/hbrowser#readme) 負責登入與瀏覽器操作，
[h2hdb-ingest](https://github.com/Kuan-Lun/h2hdb-ingest#readme)
負責處理 H@H 收到的檔案。

## 使用前準備

- Python 3.14 以上版本，以及 HBrowser 支援的瀏覽器環境。
- 可登入目標網站的 E-Hentai 帳號、在線的 H@H 客戶端，以及網站要求的下載額度或費用。
  使用 ExHentai 時，帳號也必須具有該站存取權。
- 已初始化、schema 相容的 h2hdb 資料庫與其 `h2hdb-config.json`。
- 使用同一個資料庫、持續執行的 ingest 程序，讓下載批次可以交接並繼續處理。

請先依 [h2hdb 的使用說明](https://github.com/Kuan-Lun/h2hdb#readme)
準備資料庫與設定檔。Downloader 不會替你建立或升級資料庫；舊資料庫被拒絕時，
先依 Core 文件確認適用的離線升級方式。

## 安裝與帳號設定

在這份 checkout 的根目錄建立虛擬環境並安裝套件。

macOS／Linux：

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Windows PowerShell：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

安裝程式會一併解析依賴；目前宣告的 Core 範圍為 `h2hdb>=0.43.0,<0.46.0`，
HBrowser 範圍為 `hbrowser>=0.44.0,<0.45.0`，完整內容見
[pyproject.toml](pyproject.toml)。

在執行腳本的終端機設定瀏覽器帳號。請填入自己的帳密，避免將密碼寫進程式碼或
提交到版本庫。

macOS／Linux：

```bash
export EH_USERNAME='your_username'
export EH_PASSWORD='your_password'
export USE_TOR=0
```

Windows PowerShell：

```powershell
$env:EH_USERNAME = 'your_username'
$env:EH_PASSWORD = 'your_password'
$env:USE_TOR = '0'
```

`USE_TOR=0` 使用直接連線；未設定時，HBrowser 可能自動使用本機偵測到的 Tor。
以下範例會顯示瀏覽器視窗，方便第一次登入時手動完成驗證，因此需要圖形桌面。
無視窗模式與可選的 FlareSolverr 設定請見 HBrowser 文件。

## 第一次執行：下載 CSV 中的圖庫

### 1. 準備清單

建立 UTF-8 編碼的 `todownload_gids.csv`，將範例 GID 與網址換成要下載的圖庫：

```csv
gid,url
123,https://exhentai.org/g/123/456/
666,
,https://exhentai.org/g/789/abc/
```

每列必須恰好有 `gid`、`url` 兩欄。GID 必須是正整數；只有 GID 時會透過瀏覽器查找，
只有 URL 時會從網址解析 GID，兩者都有時則必須指向同一個圖庫。

CSV 是加入工作的收件匣。匯入資料庫後，已匯入的列會從 CSV 移除；這不代表下載完成。
檔案不存在時會自動建立，但所在目錄必須已經存在。

### 2. 執行腳本

將以下內容存為 `download_queue.py`，與 `h2hdb-config.json`、
`todownload_gids.csv` 放在同一個工作目錄，然後執行 `python download_queue.py`：

```python
import asyncio

from h2hdb import VNextDownloadQueueFacade, load_config
from h2hdb_downloader import Downloader, TagCascadePolicy
from hbrowser import ExHDriver


async def main() -> None:
    facade = VNextDownloadQueueFacade(load_config("h2hdb-config.json"))
    try:
        async with Downloader(
            ExHDriver(headless=False),
            facade=facade,
            csv_path="todownload_gids.csv",
            wait4client=30 * 60,
            retry2download=4 * 60 * 60,
            download_submissions_per_ingest=100,
        ) as downloader:
            # 只處理清單中的圖庫，不展開相關作者或社團。
            policy = TagCascadePolicy(filters=(), conditions=())
            results = await downloader.drain_queue(policy)
            for gid, submitted in results.items():
                print(gid, submitted)
    finally:
        facade.close()


if __name__ == "__main__":
    asyncio.run(main())
```

若使用 E-Hentai，將匯入與建立 driver 的 `ExHDriver` 改成 `EHDriver`。
`Downloader` 會在進入與離開 `async with` 時開啟及關閉瀏覽器；傳入尚未進入 context
的 driver 即可。若應用程式已自行管理登入中的 driver，可以傳入該 driver 並直接
呼叫 downloader 方法，不要再進入一次 `async with downloader`。

### 3. 查看結果

`drain_queue()` 處理本次取得的佇列快照，每批交給 ingest 並等待它完成，最後回傳
`dict[int, bool]` 後結束。後來才加入的工作留待下次呼叫處理；腳本不會持續監看 CSV。

- `True` 表示圖庫已成功提交到 H@H，並非下載完成通知或本機檔案路徑。
- `False` 表示這次沒有成功提交，可能是略過、無法取得或其他未成功的情況。
- 檔案是否已送達及完成匯入，請查看 H@H 與 ingest 的處理狀態。

只使用資料庫內已有的待辦工作時，將 `csv_path` 設為 `None` 即可停用 CSV 收件匣。

## 相關作品與重新下載

若要沿著作者、社團標籤尋找中文或無對白的相關圖庫，將範例中的 policy 改為：

```python
policy = TagCascadePolicy(
    filters=("artist", "group"),
    conditions=("language:chinese$", "language:speechless$"),
)
```

每個 `condition` 會在每個符合的標籤底下分別搜尋，再合併結果；兩個條件不是要求
同時符合。`conditions=()` 表示不增加搜尋限制，可能展開大量作品。
`filters=()` 則不展開相關作品。

在使用中的 downloader context 內，依需求選擇方法：

| 要做的事 | 呼叫方式 |
| --- | --- |
| 處理目前資料庫與 CSV 佇列 | `await downloader.drain_queue(policy)` |
| 處理目前待重新下載的清單 | `await downloader.drain_pending_redownloads(policy)` |
| 下載單一 GID 與相關作品 | `await downloader.deep_download_by_gid(gid, policy)` |
| 下載已知網址的圖庫與相關作品 | `await downloader.deep_download_by_gallery(gallery, policy)` |
| 只查看待重新下載的 GID | `downloader.pending_redownload_gids()` |

網址形式的 `gallery` 請用 `h2h_galleryinfo_parser.GalleryURLParser(url)` 建立。
Deep 方法通常在主圖庫成功提交後才展開相關標籤；設定 `skip_check=True` 可在主圖庫
被略過時仍展開。兩個 `drain_*` 方法預設已啟用這個選項。

預設每批至少接受 100 個不重複圖庫的提交後，或本次快照已處理完時，交接給 ingest。
`download_submissions_per_ingest` 可以調整這個門檻；主圖庫與其相關作品會先處理完，
才檢查門檻，因此一批可能超過設定數量。這個數字計算成功提交的圖庫，不計算檔案數
或等待時間。

若應用程式自行安排 ingest 時機，也可使用 `download_by_gallery(gallery)`、
`download_by_gid(gid)`、`download_by_tag(tag, conditions)`。這些直接呼叫仍使用
資料庫佇列，但不取得下載輪次，也不等待 ingest；需要與 ingest 協調時請使用上述
`deep_*` 或 `drain_*` 方法。

## 等待、重試與中斷恢復

建立 `Downloader` 時必須提供 `wait4client` 與 `retry2download`，單位都是秒：

| 參數 | 行為 |
| --- | --- |
| `wait4client` | H@H 客戶端離線時，等待多久再試；`0` 會立即拋出例外。 |
| `retry2download` | 帳號額度不足時，等待多久再試；`0` 會立即拋出例外。 |
| `turn_poll_seconds` | 等待下載輪次或 ingest 完成的查詢間隔，預設 `5`。 |
| `turn_lease_seconds` | 下載輪次的租約秒數，預設 `300`。 |
| `turn_heartbeat_seconds` | 更新租約的間隔，預設 `60`，必須小於租約秒數。 |

單次圖庫提交遇到上述離線或額度例外時，最多會在等待後額外嘗試一次；若再次失敗，
會向呼叫端拋出例外。登入、驗證、搜尋或其他失敗不套用這兩個等待設定。
下載輪次與 ingest 等待則會按 `turn_poll_seconds` 持續輪詢，所以 ingest 必須能持續
取得並完成工作。

輪詢與心跳間隔必須是有限正數；租約與 `download_submissions_per_ingest` 必須是
正整數。一般使用可保留預設值。

未完成的佇列工作可在修復原因後重新執行 `drain_queue()`。只有經明確確認不存在的
圖庫才會標記移除，不能將登入或搜尋失敗當成圖庫已刪除。已收錄的圖庫通常會略過，
但明確的下載要求、待重新下載標記或定期重新檢查仍可能觸發提交。

程式也可能在 H@H 接受下載後、尚未保存結果前中斷，因此重啟後可能再次提交同一圖庫。
此流程允許重複提交，不保證只提交一次。若收到 HBrowser 的
`ArchiveDownloadOutcomeUnknownError`，先檢查 H@H 狀態再決定是否重試。

CSV 旁的 `.todownload_gids.csv.claim-*` 隱藏檔代表尚未完成的匯入，下一次會自動重播。
請保留這些檔案；格式有誤時修正報錯的 CSV 或 claim 檔，再重新執行。

## 常見問題

| 問題 | 處理方式 |
| --- | --- |
| 一直等待下載輪次或 ingest | 確認 ingest 正在使用相同資料庫，且工作確實有進展。 |
| `DownloadTurnLostError` | 結束本次操作，檢查是否有競爭中的 worker 或租約更新延遲；未完成工作可留待下次處理。 |
| 登入或驗證失敗 | 使用 `headless=False` 查看頁面，確認帳號與連線方式，再依 HBrowser 文件排錯。 |
| H@H 離線或額度不足 | 先恢復客戶端或補足額度；若要自行處理例外，將對應等待參數設為 `0`。 |
| CSV 解析失敗 | 檢查兩欄格式、正整數 GID、URL／GID 是否一致，以及保留的 claim 檔。 |
| 資料庫 schema 被拒絕 | 依 h2hdb 文件檢查初始化或離線升級流程，Downloader 不會升級資料庫。 |

瀏覽器診斷與可選日誌設定請見 HBrowser 文件。回報問題時請附套件版本、失敗的操作與
移除私人資訊後的例外訊息，勿附上帳密或私人帳號頁面。問題可提交到
[issue tracker](https://github.com/Kuan-Lun/h2hdb-downloader/issues)。

## 授權

本專案採用 GPL-3.0-only，詳見 [LICENSE](LICENSE)。
