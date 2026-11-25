# Naturalia 獨立測試版本

此版本由零開發，沒有沿用 sample 網站或 Staff AI Agent 程式碼。按用戶指示，只沿用 Staff AI Agent 的 Kimi API 設定。客人自助會員及交易服務未開通；B2B 管理後台的充值核款測試版已實作，見 [後台使用說明](ADMIN_README.md)。

## 啟動

```sh
chatbot/.venv/bin/python chatbot/server.py
```

- 聊天頁：http://127.0.0.1:8765
- 嵌入示範：http://127.0.0.1:8765/demo
- 健康檢查：`GET /api/health`

另一台電腦首次安裝：

```sh
python3 -m venv chatbot/.venv
chatbot/.venv/bin/pip install -r chatbot/requirements.txt
chatbot/.venv/bin/python chatbot/setup_models.py
```

Kimi 設定放在 `chatbot/.env`，欄位參考 `.env.example`。此檔已忽略，權限為 600；不應提交、分享或放到前端。現有設定已從用戶指定的 BizConnect 專案讀入，不修改該專案。

## 已實作

- 188 件商品及真實來源、SKU、圖片、日期、存檔價格。
- 本機多語言向量檢索，結合字詞排序、同義詞、常見錯字、套裝及排除條件。
- 向量索引依商品內容雜湊保存；資料變更後啟動時重建。
- Kimi RAG：向模型提供候選商品及近期對話，要求生成商品選擇及原文引用。後端核對 ID 及引文確實存在才展示；無效回答降級為搜尋結果。這個版本刻意使用來源摘錄，不展示未核實的自由生成商品功效。
- 運送、退換貨、隱私及商店介紹政策查詢與來源連結。
- 獨立聊天介面、商品卡片、快捷服務、手機排版、iframe 嵌入按鈕。
- SQLite 訪客對話保存與恢復，隨機 session token、帳戶外的訪客對話隔離、重新開始刪除對話；有效期 7 日，建立新 session 時清理過期資料。
- Kimi 超時／無效引文降級、輸入大小限制、同來源請求驗證、單次推理鎖、session 建立頻率限制。

會話 token 在目前瀏覽器分頁 sessionStorage；刷新可以恢復，關閉分頁後通常不再保留。不是會員登入或跨裝置同步。SQLite 僅供本機測試，檔案不是加密儲存。

## 嵌入方式

先以 `/demo` 測試，正式網站接入需部署 HTTPS 後使用：

```html
<script src="https://YOUR-CHATBOT-HOST/widget.js" defer></script>
```

iframe 隔離店面樣式，API 請求由 iframe 同來源發出，不需要向店面公開模型 Key。正式嵌入仍須店方允許 script／iframe 及完成瀏覽器驗收。

## 資料與模型

商品及政策來自 2026-11-16 公開存檔；不讀取會員資料備份。價格不能當作現價，庫存不作推算。Kimi 會收到客人問題、近期對話及候選商品資料；本機向量模型不外傳查詢內容。

語意模型：[paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)，[編碼 API](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)。生成回答使用已設定的 Kimi 模型名稱，不由程式猜測。模型供應商契約參考 [Kimi 官方說明](https://www.kimi.ai/resources/model-api)。

## 驗證

```sh
chatbot/.venv/bin/python -m unittest discover -s chatbot/tests -v
chatbot/.venv/bin/python chatbot/tests/evaluate_semantic.py
node --check chatbot/static/app.js
node --check chatbot/static/widget.js
```

上述測試不調用付費模型。Kimi 使用假的 HTTP 回應測試有效引用、捏造引用、服務失敗及安全降級；語意評測使用本機模型與真實商品 ID。報告存於 `runtime/semantic-evaluation.json`。

按來源專案 AGENTS.md，付費模型驗證須在 Chrome 逐條提問並檢查回答，不做自動批量付費測試。本次瀏覽器控制服務不可用，故真實 Kimi 回答及瀏覽器視覺驗收尚未完成；「已設定」不代表已連線實測成功。

## 超出目前獨立測試範圍

SHOPLINE API／會員 OAuth、即時價錢與庫存、會員訂單與餘額、付款核實、充值、扣款、退款、正式部署。這些入口明示未開通，不會假裝交易成功。

通用錯字、多輪條件替換、品牌與預算的複雜組合仍需更多實際案例評測。此版本不是生產服務；正式部署需 HTTPS、認證、資料保留規則、併發與負載處理及完整安全檢查。

## WhatsApp 掃碼通道

已新增獨立 [WhatsApp 管理與連線服務](whatsapp/README.md)，本機入口 http://127.0.0.1:8767 。與網站共用 RAG 及 Kimi，會員身份不自動合併。
