# Naturalia 網站 API 調查

調查日期：2026-11-24（香港時間）

Naturalia 使用 SHOPLINE 店面。與本專案相關的接口分為網站前端內部 API、官方 Storefront SDK，以及需要商戶授權的 Open API。官方文件涵蓋商品、會員身份、訂單和購物金操作；尚未取得 Naturalia 商戶 API 授權，不能將平台支援的功能視為此商店已成功串接。

**需求修正（2026-11-24）**

用戶已確認充值屬獨立制度，應由自有帳本處理。下述 SHOPLINE store credits 接口只作平台能力紀錄，不用於本專案充值／扣款；舊表格的充值對應方案已被 [後台計劃](docs/chatbot-plan/ADMIN_PLAN.md) 取代。

**調查證據及驗證範圍**

- 網頁讀取工具可讀取 [Naturalia 首頁](https://www.naturalia-group.com/)。互動瀏覽器主機不可用，因此沒有登入後的 Network／XHR 實測。
- 對首頁、商品讀取接口及分類接口進行未登入 GET，均收到 HTTP 403 的 HTML 忙碌頁，未取得 API JSON。
- 未攜帶 Token 的 Open API 商品 GET 亦收到 HTTP 403 HTML。這不足以判定是 API 授權層或前置網絡層拒絕，不能解讀成正式的 API 錯誤回應。
- 從 2026-11-16 的 Naturalia 網頁備份取得前端程式 URL。本次重新下載四個相關 CDN JavaScript 檔，全部 HTTP 200，內容與備份相同。下列內部路由屬於程式碼證據，未完成執行時驗證。
- 備份首頁 `window.mainConfig` 的公開 merchant ID 是 `6878c0556ba1e4002f6fe02a`。這是識別碼，不是登入憑證。
- 本次沒有登入帳戶、建立訂單、發送訊息、修改購物車、充值或扣款。

本地證據：[下載與調查記錄](.cache/naturalia-api-review/review-evidence.json)、[GET 測試結果](.cache/naturalia-api-review/read-only-checks.json)、[前端接口程式片段](.cache/naturalia-api-review/cached-api-references.json)。

**網站前端內部接口**

以下路由以 `https://www.naturalia-group.com` 為基底。`{merchantId}`、`{productId}`、`{categoryId}`、`{orderId}` 是參數位置。讀寫方法均來自前端程式，表內的 POST／PUT／DELETE 只作識別，沒有實際執行。

| 方法 | 路由 | 程式中顯示的用途 |
| --- | --- | --- |
| GET | `/api/categories/{categoryId}/products` | 分類商品，包含 `limit`、`page`、`offset` 參數 |
| GET | `/api/merchants/{merchantId}/products/{productId}` | 單一商品資料 |
| POST | `/api/products/list_products` | 批量取得商品，具體請求格式仍待核對 |
| GET | `/api/merchants/{merchantId}/products/{productId}/check_stock` | 規格庫存檢查，程式傳入 `variation_id` 等參數 |
| POST | `/api/merchants/{merchantId}/products/check_stocks/` | 批量庫存檢查 |
| GET | `/api/merchants/{merchantId}/cart` | 目前購物車；程式讀取回應中的 `user_credit_balance`、`applied_user_credits` |
| GET | `/api/merchants/{merchantId}/cart/count` | 購物車數量 |
| POST | `/api/merchants/{merchantId}/cart/items` | 加入購物車 |
| PUT | `/api/merchants/{merchantId}/cart` | 更新購物車結帳選項等資料 |
| DELETE | `/api/merchants/{merchantId}/cart/items?item_id={itemId}` | 移除購物車項目 |
| POST | `/api/orders/checkout` | 提交結帳，屬於會產生交易結果的操作 |
| GET | `/api/orders/{orderId}` | 訂單資料，程式引用目前登入用戶 |
| GET | `/api/users/promotions?scope={scope}` | 用戶優惠券 |
| GET | `/api/user_credit_rules` | 購物金規則，不能當作購物金餘額接口 |
| GET | `/api/member_points` | 會員點數記錄 |
| GET | `/api/wish_list_items` | 追蹤清單；同一路由另有新增及刪除操作 |
| POST | `/api/messages` | 商店聯絡訊息，不能視為自建 AI 聊天紀錄接口 |

另發現 `/api/theme/v1/merchants/{merchantId}`、配送選項、商品評價及 app metafields 等路由。共用程式也可能包含此店未啟用的功能，因此不能單憑路由存在判斷店方可用功能。

這些路由不是本次核對的官方 Open API 契約。涉及帳戶或購物車的請求可能依賴站內登入狀態、Cookie、頁面設定及驗證流程；跨網域權限與實際回應尚未驗證。正式串接優先採用下述官方接口。

前端程式來源：

- [商品分類及庫存服務](https://cdn.shoplineapp.com/packs/js/shared~dbe7acde-2be52db35520d059bbcc.chunk.js)
- [購物車及結帳服務](https://cdn.shoplineapp.com/packs/js/shared~d54d7c1c-2a97b22cc339268bc530.chunk.js)
- [商品讀取及會員相關服務](https://cdn.shoplineapp.com/packs/js/shared~9ee0282b-8f41f8569608eb0fc578.chunk.js)
- [App Extension SDK](https://cdn.shoplineapp.com/packs/js/app_extension_sdk-16c697cb5a5ad03a2c9d.chunk.js)

**官方 Open API**

基底為 `https://open.shopline.io/v1`。下列接口已核對官方文件；此商店的 Token、API 權限及實際回應尚未驗證。

| 方法 | 路由 | 對聊天插件的用途 | 官方文件 |
| --- | --- | --- | --- |
| GET | `/products` | 同步商品目錄 | [商品清單](https://open-api.docs.shoplineapp.com/reference/get_products-1) |
| GET | `/products/{id}` | 取得單件商品資料 | [單件商品](https://open-api.docs.shoplineapp.com/docs/get-product) |
| GET | `/categories` | 取得分類 | [商品分類](https://open-api.docs.shoplineapp.com/docs/get-categories) |
| GET | `/customers/{id}` | 顧客資料；購物金餘額欄位以 `credit_balance` 為準 | [顧客資料](https://open-api.docs.shoplineapp.com/docs/get-customer)、[欄位異動說明](https://open-api.docs.shoplineapp.com/docs/coming-soon) |
| GET | `/customers/{id}/store_credits` | 顧客購物金歷史 | [購物金歷史](https://open-api.docs.shoplineapp.com/reference/get_customers-id-store-credits-1) |
| POST | `/customers/{id}/store_credits` | 增加或扣減購物金 | [購物金調整](https://open-api.docs.shoplineapp.com/docs/update-customer-store-credits) |
| GET | `/orders?customer_id={id}` | 查詢指定顧客訂單 | [訂單清單](https://open-api.docs.shoplineapp.com/reference/get_orders-1) |
| GET | `/orders/{id}` | 查詢單筆訂單 | [單筆訂單](https://open-api.docs.shoplineapp.com/docs/get-order) |
| GET | `/orders/search` | 按條件搜尋訂單 | [搜尋訂單](https://open-api.docs.shoplineapp.com/docs/search-orders) |
| POST | `/orders` | 建立訂單，付款、庫存及購物金扣款流程須另行驗證 | [建立訂單](https://open-api.docs.shoplineapp.com/docs/create-order) |

購物金調整接口接受 `value`、`remarks`，並提供到期日 `expires_at` 等欄位。它是餘額調整能力，不代表已完成收款，也不等於一個完整的購買交易。充值入帳須連接已確認收款的流程；購買扣款亦須與訂單成功、退款及重試處理協調，避免重複入帳或重複扣款。

**取得商戶授權的兩條路徑**

1. 單店串接：依官方 [取得 access token 指引](https://open-api.docs.shoplineapp.com/docs/getting-started)，先向 SHOPLINE 申請 OpenAPI 使用權限，店方才會在管理員設定看見 API Auth。選擇要授權的 API 後產生 Token。Naturalia 是否已開通，尚未確認。
2. 可重用的第三方插件：透過 Developer Center 建立 App、設定所需 scopes，由商戶安裝授權，再透過 OAuth 或 App Installation Token 取得 Open API Token。[官方 App Token 說明](https://shopline-developers.readme.io/docs/api-keys)

官方請求範例要求 `Authorization: Bearer ...`、`Accept: application/json` 及識別整合方的 `User-Agent`。Open API 日期時間採 UTC，顯示香港日期時需換算。[請求格式](https://open-api.docs.shoplineapp.com/docs/openapi-request-example)

商戶 Token 供自建後端使用，不應放入網站前端聊天程式。商戶 Token 與顧客登入身份是兩件事，不能只依聊天端傳入的 customer ID 就讀取該會員資料。

**會員登入及恢復對話**

官方提供店面顧客 OAuth：

| 方法 | 所在網域及路由 | 用途 |
| --- | --- | --- |
| POST | `open.shopline.io/v1/storefront/oauth_applications` | 商戶授權後建立 Storefront OAuth App |
| GET | `www.naturalia-group.com/oauth/authorize` | 導向顧客授權流程 |
| POST | `www.naturalia-group.com/oauth/token` | 後端以授權碼交換顧客 Token |
| GET | `www.naturalia-group.com/oauth/token/info` | 驗證 Token 並取得登入顧客資料 |

以上是依 [官方顧客認證流程](https://shopline-developers.readme.io/docs/integration-guide-for-merchant-customer-authentication) 對應本店網域的路由，尚未在 Naturalia 建立 App 或執行流程。

對本專案的建議是：登入驗證後，以商戶 ID 和顧客 ID 對應自建聊天紀錄，實現跨次造訪恢復對話。本次沒有找到可直接恢復本專案 AI 對話的現成 SHOPLINE API。

**適合嵌入式聊天插件的 Storefront SDK**

Naturalia 備份頁面引用了 App Extension SDK，本次可從 CDN 下載相同程式。官方初始化入口是 `window.shopline.appExtension.init({ appId })`。需要店方安裝 App、啟用 App script，並符合 SDK 支援的主題與功能條件；程式檔存在不代表任意 appId 都能使用。[啟用條件](https://shopline-developers.readme.io/docs/storefront-sdk-introduction)、[初始化流程](https://shopline-developers.readme.io/docs/setting-up-storefront-sdk)

| SDK 方法或事件 | 功能及限制 |
| --- | --- |
| `getMerchantId()` | 取得商戶識別碼 |
| `getCurrentPageIdentifier()` | 判斷目前頁面類型 |
| `getPDPProduct()` | 商品詳情頁的商品識別及相關資訊，不是全店商品查詢 |
| `getPLPProducts()` | 商品列表頁的商品資訊 |
| `getCPCartItems()`、`getCOPCartItems()` | 分別讀取購物車頁及結帳頁的購物車資訊 |
| `addCartItem()`、`addCartItems()` | 在支援的頁面加入商品 |
| `updateCartItemQuantity()`、`removeCartItem()` | 在支援的頁面修改或移除購物車項目 |
| `cart.update`、`page.variationChanged` | 監聽購物車或商品規格變化 |

SDK 的購物車修改方法有頁面限制，官方目前列明 Cart、Checkout、One Page Store 等不支援的情況。使用前必須檢查頁面及功能可用性。[SDK 方法及事件](https://shopline-developers.readme.io/docs/storefront-sdk-list-of-available-functions-events)

官方亦另有 Storefront API Beta，訪客使用 Storefront Access Token，登入顧客再加 Customer Access Token。它與上述瀏覽器 SDK 不同；目前未確認本店開通狀態。[Storefront API 說明](https://shopline-developers.readme.io/docs/introduction_storefront_api)

**官方已公告的欄位變更**

- 預計 2027-01-01：Get Customers Store Credits 及 Update Customer Store Credits 回應移除 `credit_balance`，查目前餘額改用 Get Customer。不能將歷史購物金記錄的回應當作長期穩定的餘額來源。
- 預計 2027-04-01：公告列出的顧客、訂單等清單接口將移除 `page` 分頁，改用 `previous_id`；優惠券則有獨立的 cursor 版本。不能把這項變更概括套用到所有接口。

日期是官方預計上線時間，實作前需再核對 [Breaking Changes 公告](https://open-api.docs.shoplineapp.com/docs/coming-soon)。

**與本專案需求的對應**

| 需求 | 可採用的方式 | 尚需確認 |
| --- | --- | --- |
| 客服知識及商品推薦 | 現有網站資料，加上授權 Open API 商品同步 | 商品可售狀態、價格更新規則 |
| 跳到商品詳情購買 | 使用現有商品 URL | 已有資料可支援 |
| 聊天視窗加入購物車 | 官方 Storefront SDK | App、主題及頁面支援 |
| 登入後恢復對話 | 顧客 OAuth，加上自建聊天儲存 | OAuth App 及對話後端 |
| 查自己的訂單及餘額 | 後端驗證顧客身份後呼叫 Open API | 商戶 Token 及讀取權限 |
| 充值入帳 | 收款確認流程，加上購物金調整接口 | 支付通知、人工核款安排、優惠及到期規則 |
| 充值餘額購買扣款 | 訂單及購物金交易協調 | 結帳實測、重複請求及退款處理 |

下一步可先確認店方有否 API Auth 或可安裝 Developer Center App，取得最小所需的讀取權限後驗證商品、指定會員餘額及訂單。寫入購物金及建立訂單則要先確定業務規則與測試帳戶。
