from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import re
import logging

# ログの設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="利回りウォッチャー 株価取得API",
    description="日本株の最新株価と配当利回りを取得するAPIサーバー",
    version="1.3.0"
)

# CORS設定（すべてのオリジンからのリクエストを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# レスポンスのデータ構造を定義するPydanticモデル
# フロントエンド（app.js）の要求するキー名に完全に一致させています。
# Pythonの予約語である "yield" を回避するため、Pydanticのエイリアス機能を使用しています。
class StockResponse(BaseModel):
    code: str
    name: str
    price: float
    change: float
    changePercent: float = Field(default=0.0, serialization_alias="changePercent", validation_alias="changePercent")
    dividend: float
    yield_val: float = Field(default=0.0, serialization_alias="yield", validation_alias="yield")
    source: str

def scrape_fallback(code: str):
    """
    yfinanceでデータが取得できなかった場合のフォールバックとして、
    Yahoo!ファイナンス（日本）のHTMLをパースして株価、配当利回り、銘柄名などを取得します。
    """
    # 銘柄コードから数字部分のみを抽出 (例: 2914.T -> 2914)
    clean_code = re.sub(r"\D", "", code)
    if not clean_code:
        return 0.0, 0.0, "", 0.0

    url = f"https://finance.yahoo.co.jp/quote/{clean_code}.T"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    price = 0.0
    dividend_yield = 0.0
    name = ""
    dividend = 0.0
    
    try:
        logger.info(f"Scraping Yahoo Finance Japan for code: {clean_code}")
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Failed to fetch Yahoo Finance Japan page: status {response.status_code}")
            return 0.0, 0.0, "", 0.0
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # --- 1. 銘柄名の取得 ---
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            # 「日本たばこ産業(株)【2914】...」から「日本たばこ産業(株)」を抽出
            name_match = re.search(r"^(.+?)(?:【|\()", title_text)
            if name_match:
                name = name_match.group(1).strip()
        
        # --- 2. JSON-LD (構造化データ) からの株価抽出を優先試行 ---
        json_ld_tags = soup.find_all('script', type='application/ld+json')
        for tag in json_ld_tags:
            try:
                import json
                data = json.loads(tag.string)
                if isinstance(data, list):
                    items = data
                else:
                    items = [data]
                    
                for item in items:
                    if isinstance(item, dict):
                        p_val = item.get("price")
                        if p_val is not None:
                            price = float(p_val)
                            logger.info(f"Price found in JSON-LD: {price}")
                            break
                if price > 0.0:
                    break
            except Exception as json_err:
                logger.debug(f"JSON-LD parse error: {json_err}")
                continue

        # --- 3. HTML要素からの株価抽出 (JSON-LDで取れなかった場合) ---
        if price == 0.0:
            price_candidates = []
            span_tags = soup.find_all('span', class_=re.compile(r'(_3rXW2W1c|value|price|kobetsu-value)'))
            for span in span_tags:
                text = span.get_text(strip=True).replace(',', '')
                if re.match(r'^\d+(\.\d+)?$', text):
                    price_candidates.append(float(text))
            
            if price_candidates:
                price = price_candidates[0]
                logger.info(f"Price found in span class: {price}")

        # --- 4. 配当利回りの取得 ---
        # 「配当利回り」というラベルを持つ要素を検索
        dt_tags = soup.find_all(['dt', 'th', 'td', 'span'], string=re.compile("配当利回り"))
        for dt in dt_tags:
            dd = dt.find_next(['dd', 'td'])
            if dd:
                val_text = dd.get_text(strip=True)
                # 「5.20%」などのテキストから数値を抽出
                match = re.search(r"([\d\.]+)\s*%", val_text)
                if match:
                    dividend_yield = float(match.group(1))
                    logger.info(f"Dividend yield found in scraping: {dividend_yield}%")
                    break
                    
        # --- 5. 1株配当金の取得 ---
        # 「1株配当」というラベルを持つ要素を検索
        div_dt_tags = soup.find_all(['dt', 'th', 'td', 'span'], string=re.compile("1株配当"))
        for dt in div_dt_tags:
            dd = dt.find_next(['dd', 'td'])
            if dd:
                div_text = dd.get_text(strip=True)
                # 「192.00円」などのテキストから数値を抽出
                div_match = re.search(r"([\d\.]+)", div_text)
                if div_match:
                    dividend = float(div_match.group(1))
                    logger.info(f"Dividend found in scraping: {dividend}")
                    break
                    
        return price, dividend_yield, name, dividend
    except Exception as e:
        logger.error(f"Error occurred during scraping fallback: {e}")
        return price, dividend_yield, name, dividend

@app.get("/")
def read_root():
    return {"message": "Yield Watcher API is running. Use /api/stock?code=XXXX to fetch stock data."}

@app.get("/api/stock", response_model=StockResponse)
def get_stock(code: str = Query(..., description="銘柄コード (数字4桁、または末尾に.T付きのコード。例: 2914, 7203.T)")):
    """
    指定された銘柄コードの最新株価と配当利回りを取得します。
    フロントエンド（app.js）が要求する「yield」「changePercent」等の命名規則に完全に準拠して返却します。
    """
    # コードの正規化
    code = code.strip().upper()
    
    # yfinance用の証券コード整形（日本株は末尾に .T が必要）
    yf_code = code
    if len(code) == 4 and code.isdigit():
        yf_code = f"{code}.T"
        
    logger.info(f"Requested code: {code} (yfinance code: {yf_code})")
    
    name = ""
    price = 0.0
    change = 0.0
    change_percent = 0.0
    dividend = 0.0
    dividend_yield = 0.0
    source = "yfinance"
    
    yfinance_success = False
    
    # 1. yfinance からのデータ取得を試行
    try:
        ticker = yf.Ticker(yf_code)
        info = ticker.info
        
        if info:
            # 銘柄名
            name = info.get("longName") or info.get("shortName") or ""
            
            # 株価の確実な取得（複数のキーを優先度順に確認）
            for key in ["currentPrice", "regularMarketPrice", "previousClose", "open"]:
                val = info.get(key)
                if val is not None:
                    try:
                        price = float(val)
                        break
                    except ValueError:
                        continue
            
            # 前日比・前日比％・1株配当
            change = float(info.get("regularMarketChange") or 0.0)
            change_percent = float(info.get("regularMarketChangePercent") or 0.0)
            dividend = float(info.get("dividendRate") or 0.0)
            
            # 配当利回りの取得とクレンジング
            dy = info.get("dividendYield")
            if dy is not None:
                try:
                    dy_val = float(dy)
                    
                    # 単位・異常値の自動判定ロジック:
                    # 一貫して「3.92」のようなパーセンテージ表記(float)に統一します。
                    if dy_val == 0.0:
                        dividend_yield = 0.0
                    # A. 小数表記の場合 (例: 0.0392 -> 3.92%)
                    elif dy_val < 0.2:
                        dividend_yield = dy_val * 100
                    # B. すでにパーセント表記の場合 (例: 3.92 -> 3.92%)
                    elif dy_val < 20.0:
                        dividend_yield = dy_val
                    # C. 異常に大きな値の場合 (例: 192 や 392 など、配当金額そのものが混入している可能性)
                    else:
                        divided_val = dy_val / 100
                        if divided_val < 20.0:
                            dividend_yield = divided_val
                        else:
                            logger.warning(f"Abnormal dividend yield value from yfinance: {dy_val}. Forcing fallback.")
                            dividend_yield = 0.0
                            
                    # 最低限、株価が取得できており、配当利回りも正常範囲で取得できればyfinance成功と判定
                    yfinance_success = (price > 0.0 and dividend_yield > 0.0)
                except ValueError:
                    pass
    except Exception as e:
        logger.error(f"yfinance lookup failed for {yf_code}: {e}")
        
    # 2. yfinanceでの取得が失敗、または配当利回りが 0.0（あるいは取得不可）の場合、スクレイピングによるフォールバックを実行
    if not yfinance_success or price == 0.0 or dividend_yield == 0.0:
        logger.info(f"Data incomplete or suspicious from yfinance. Attempting fallback scraping for {code}...")
        scraped_price, scraped_dy, scraped_name, scraped_dividend = scrape_fallback(code)
        
        # 銘柄名の補完
        if not name and scraped_name:
            name = scraped_name
            
        # 株価の更新: スクレイピングで取れた最新の日本市場株価を優先
        if scraped_price > 0.0:
            price = scraped_price
            source = "scraping"
            
        # 配当利回りの更新
        if dividend_yield == 0.0 and scraped_dy > 0.0:
            dividend_yield = scraped_dy
            source = "scraping"
            
        # 1株配当金の更新
        if dividend == 0.0 and scraped_dividend > 0.0:
            dividend = scraped_dividend

    # 銘柄名がどうしても空の場合は仮の名称を設定
    if not name:
        name = f"銘柄コード: {code.replace('.T', '')}"

    # 株価がどうしても取得できなかった場合はエラーとする
    if price == 0.0:
        raise HTTPException(
            status_code=404, 
            detail=f"銘柄コード {code} の株価データを取得できませんでした。コードが正しいか確認してください。"
        )
        
    # float型であることを保証してレスポンススキーマに沿って返却
    # 辞書のキー名は Pydantic の validation_alias にマッピングされます。
    return {
        "code": code.replace(".T", ""),
        "name": name,
        "price": float(price),
        "change": float(change),
        "changePercent": float(change_percent),
        "dividend": float(dividend),
        "yield": float(dividend_yield),  # validation_alias により Pydantic モデルの yield_val にバインドされます
        "source": source
    }
