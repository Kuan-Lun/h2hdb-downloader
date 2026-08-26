# AGENTS.md

## 政策來源

- 本檔是此 repository 的唯一代理開發政策來源。
- 其他代理入口只能要求完整閱讀本檔，不得複製另一份政策。
- 可執行規則以 repository 內的 scripts 與設定檔為準。

## 溝通

- 最終回覆一律使用繁體中文。
- 程式碼、識別字、命令、檔名與 commit message 可使用英文。
- 不得為了承載回覆而新增 Markdown 文件。
- 移除 compatibility path、改變公開行為或採用例外時，必須在對話及
  最終回覆中明確說明。

## 設計與修改原則

- 不預設存在最小修改或向後相容要求。
- 在任務範圍內選擇架構、可讀性與可測試性最好的完整結果。
- 綜合考慮 SOLID、KISS、YAGNI、內聚性與低耦合。
- 必要的局部重構可直接納入任務。
- 若會實質擴大任務範圍、改變原要求未涵蓋的公開行為，或引入資料遷移，
  必須先取得使用者同意。
- 任務直接涉及的 legacy compatibility code 應移除，不保留 shim；不全面
  清理與任務無關的 legacy code。
- generated output 不得直接修改；必須修改 generator 或 source 後重新產生。

## 工作樹與 Git

- 唯讀分析不建立 branch。
- 凡會修改 tracked files 的任務，使用
  `scripts/detect-primary-branch.sh` 判定 primary，並建立專用 task branch。
- 不得 stash、reset、clean、覆寫或混入既有使用者修改。
- 工作樹不乾淨時，從 committed primary 建立獨立 worktree。
- task branch 可包含多個邏輯 Conventional Commits。避免巨大 commit；小而
  內聚的任務仍可只有一個 commit。
- 任務完成後執行 `scripts/git-flow-merge.sh`。該腳本負責完整 gate、
  `--no-ff` merge、安全移除 task worktree，以及以 `git branch -d`
  刪除已合併的本機 branch。
- primary 在任務期間可以推進；整合只要求 primary 與 task branch 有共同
  ancestor，不要求 task branch 仍直接基於目前 primary tip。
- merge conflict 或 gate failure 時必須 abort merge 並保留 task branch。
- merge 後收到的任何 follow-up 都建立新的 task branch。
- 本機 task branch、commit、`--no-ff` merge 與 `branch -d` 已獲預先
  授權。
- fetch、pull、push、remote branch、tag、release、publish、deploy 與任何
  force 操作仍須逐次明確授權。
- 不得使用 `--no-verify`。

## 提交格式

- 所有非 merge commit 必須符合 Conventional Commits。
- Breaking change 使用 `type!:` 或 `BREAKING CHANGE:` footer。
- project version 更新使用獨立 commit：
  `chore(release): bump version to X.Y.Z`。

## 版本政策

- `pyproject.toml` 的 `[project].version` 是唯一 project version source。
- project version 固定使用 `X.Y.Z`。
- 1.0 前，`Y` 是 compatibility lane，`Z` 是同一 lane 內的相容 release
  counter。相容修正或功能遞增 `Z`；breaking change 遞增 `Y` 並將
  `Z` 歸零。
- 1.0 後使用標準 Semantic Versioning。
- 整個 task branch 只在整合前更新一次 project version。
- shipped runtime 或 deployment surface 有變更時，至少需要相容升版。
- Breaking API、CLI、config、schema、protocol、資料格式或 Python/platform
  support 變更必須提高 compatibility lane 或 major。
- tests、一般文件、IDE、hooks、CI 與 dev-only tooling 單獨變更時不升版。
- 未分類路徑必須明確判定 impact，不得靜默當作 `none`。
- `Version-Impact: none` 必須附具體理由，並在最終回覆揭露。
- project version 變更必須觸發完整 direct dependency audit。
- `scripts/check-version.py` 以 staged merge candidate 判定 release surface，
  並強制 task-level `X.Y.Z` 只升版一次；pre-merge gate 不得略過。
- 升版後執行 `scripts/audit-dependencies.py --review-note "<相容性結論>"`，
  並將 `.release/dependency-audit.json` 納入 task branch。receipt 必須符合
  candidate version 與完整 dependency manifest。

## 依賴與環境

- repository 必須能從單一乾淨 checkout 重建，不得依賴固定 sibling clone
  路徑。
- 明確跨 repository 任務可使用傳入的 wheel、Git URL/ref 或 repository
  path；sibling discovery 只能是選擇性的效能優化。
- Python registry dependencies 原則上使用 `>=` lower bound；合理 upper
  bound 與 `!=` 可以保留，但必須有相容性依據。
- 精確版本只允許經驗證且有文件理由的特殊契約。
- dependency audit 必須涵蓋 build、runtime、optional 與 development direct
  dependencies，並搜尋現有 upper bound 之外的候選版本。
- 有新版時必須檢查 release notes、驗證相容性並嘗試修正問題。
- `uv.lock` 不得成為環境重建或驗證的輸入；`scripts/rebuild-env.sh` 可
  使用 `uv venv` 與 `uv pip`，但不得使用會依賴 project lockfile 的
  同步流程。
- Node tooling 使用 `npm install --package-lock=false`，不得產生或提交
  `package-lock.json`。
- 不得依賴 system-wide lint、format、type-check 或 Markdown 工具。
- `requires-python` 使用 `>=3.14`；只有經驗證的壞版本可使用 `!=`。

## 品質工具

- `pyproject.toml` 是 Ruff 與 mypy 的唯一規則來源。
- 使用 Ruff lint 與 Ruff formatter，不使用 Black。
- Ruff 使用適合專案的嚴格規則集，不從 `ALL` 出發；每個停用規則必須
  記錄理由。
- mypy 使用標準 `strict = true`。不得保留 `mypy.ini`。
- module 例外使用精確 TOML overrides。
- `type: ignore` 必須指定 error code 並附理由。
- `noqa` 必須指定 rule code 並附理由。
- Markdown 使用 repository-local `markdownlint-cli2`。
- VS Code 使用相同設定與 repository-local environment；CLI gate 是最終
  權威，IDE diagnostics 為即時輔助。

## 檢查分層

- `scripts/format.sh`：明確執行會修改檔案的 formatter 或 fixer。
- `scripts/check-fast.sh`：離線、唯讀的 Ruff、format check、mypy 與
  markdownlint；每次非 merge commit 執行。
- `scripts/check-full.sh`：fast gate、完整測試、build、wheel smoke 及本
  repository 的特殊檢查；整合候選只跑一次。
- dependency audit 可連網，但 hooks 只驗證本機 receipt，不在 commit
  過程連網。
- GitHub Actions 只呼叫相同 scripts，並保留 trusted publishing、平台特有
  或本機無法可靠重現的檢查。
- 不使用 Claude、Codex 或其他 provider-specific Stop hooks 重複檢查。

## 測試與例外

- runtime 行為變更必須新增或更新測試；bug fix 必須有 regression test。
- 新功能涵蓋正常、邊界與錯誤路徑。
- 數值測試固定隨機種子；容許誤差需有依據。
- flaky test 視為失敗，不得以重跑掩蓋。
- 不設定跨 repository 的統一 coverage 百分比。
- live account、network、production 或 destructive probe 不得進入 hooks、
  一般 pytest 或自動 merge gate。
- `skip` 或 `xfail` 必須有理由；`xfail` 原則上使用 `strict=True`。
- 不得為通過檢查而全域放寬工具設定。

## 完成回報

最終回覆必須包含：

- 實作及公開行為變化。
- 移除的 compatibility path。
- project version 與 dependency audit 結果。
- commits 與完整檢查結果。
- primary branch 與 merge commit。
- branch/worktree 是否已清除。
- 是否仍未 push、publish 或 deploy。

## Repository-specific policy

`h2hdb-downloader` 是 injected Python library，發佈與 import 名稱分別為
`h2hdb-downloader` 與 `h2hdb_downloader`。它沒有 standalone CLI。公開
exports 是 `Downloader`、`TagCascadePolicy` 與
`DownloadTurnLostError`。

### Core and side-effect boundary

- caller 注入公開 `VNextDownloadQueueFacade`；本 package 不得建構 facade、
  載入 core config、管理 `database_gate()`、import SQL/backend/repository
  internals 或 migrate schema。
- browser/network await、retry sleep 與 tag traversal 必須留在同步 facade
  call 之外。
- 只有 `_claim_download_turn()` 與 `_wait_for_gallery_ingest()` 是
  `DownloadIngestUnavailableError` liveness retry boundary。heartbeat、
  handoff、finish、queue mutation、browser work 與其他例外不得套用此 retry。
- blank-GID CSV row 由 downloader 使用 `GalleryURLParser` 從 URL 解析成
  normalized positive GID；不得把 URL parsing 推給 core facade。

### Turn, token, and replay invariants

- public deep methods 是單一 root turn；`drain_queue()` 與
  `drain_pending_redownloads()` 以完整 root traversal 批次處理，直到至少
  接受 `download_submissions_per_ingest` 個 unique submission。soft
  threshold 只能在 indivisible root/cascade 完成後檢查。
- network work 前先 claim 到 `READY`，並讓一個 async heartbeat 跨越完整
  root 或 batch。batch root 使用 exact-token in-turn completion，最後只做
  一次 handoff；single-root finish 必須讓 request mutation 與 handoff 位於
  同一 core transaction。
- root request 只有在 root 解析與完整 related-tag cascade 成功後才能以
  exact token completion。stale、replaced 或 absent token 不得刪除較新的
  request。exception、cancellation 或 process exit 時 root 必須保持 queued。
- related download 使用 `ensure_download_request()`，不得替換 snapshot
  root token。pending-redownload drain 在任何 network work 前必須完成 typed
  keyset pagination 與 pre-seeding；nonterminal empty page 不代表 scan 結束，
  continuation 必須使用 core 回傳 cursor。
- typed GID lookup 中只有 `ConfirmedGalleryMissing` 可建立 removed marker。
  challenge、auth、navigation、pagination、malformed 或 bounded-search failure
  必須 raise 並保留 durable request。successful `GalleryFound` 會先清除
  requested/resolved GID 的 stale removed marker。
- exceptional root/batch 必須嘗試 handoff。conditional handoff 若拒絕 stale
  authority，raise `DownloadTurnLostError` 並以原例外為 `__cause__`；
  cancellation 同樣處理且不得吞掉 `CancelledError`。
- H@H submission 是不可控制的 external side effect。accepted submission 與
  database checkpoint 之間存在 crash window，因此 boundary 是 at-least-once，
  不得假裝 exactly-once 或從 H@H internal state 推測 durability。
- `download_by_gallery()`、`download_by_gid()` 與 `download_by_tag()` 是
  direct APIs，不 claim turn 也不等待 ingest。需要 backpressure 的 caller
  必須使用 deep 或 drain methods。

### Verification

- tests 必須使用 fake browser/network 與 injected core facade；不得使用真實
  account、瀏覽器、H@H network 或 production database。
- queue token、heartbeat、handoff、replay、cancellation、confirmed-missing、
  snapshot cursor 與 at-least-once boundary 變更都必須有 regression tests。
- `scripts/check-full.sh` 執行完整離線 pytest、sdist/wheel build，以及從
  installed wheel 驗證三個公開 exports。
