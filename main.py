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
    version="2.0.0"
)

# 【重要】CORS設定を最も強力かつ標準的な記述に変更
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# レスポンスのデータ構造を定義するPydanticモデル
class StockResponse(BaseModel):
    code: str
    name: str
    price: float
    change: float
    changePercent: float = Field(default=0.0, serialization_alias="changePercent", validation_alias="changePercent")
    dividend: float
    yield_val: float = Field(default=0.0, serialization_alias="yield", validation_alias="yield")
    source: str

def scrape_yahoo_finance_japan(clean_code: str):
    """
    Yahoo!ファイナンス（日本）のHTMLをパースして、日本語銘柄名、リアルタイム株価、配当利回り、1株配当金を取得します。
    """
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
        
        # --- 1. 日本語銘柄名の取得 ---
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text(strip=True)
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
        dt_tags = soup.find_all(['dt', 'th', 'td', 'span'], string=re.compile("配当利回り"))
        for dt in dt_tags:
            dd = dt.find_next(['dd', 'td'])
            if dd:
                val_text = dd.get_text(strip=True)
                match = re.search(r"([\d\.]+)\s*%", val_text)
                if match:
                    dividend_yield = float(match.group(1))
                    logger.info(f"Dividend yield found in scraping: {dividend_yield}%")
                    break
                    
        # --- 5. 1株配当金の取得 ---
        div_dt_tags = soup.find_all(['dt', 'th', 'td', 'span'], string=re.compile("1株配当"))
        for dt in div_dt_tags:
            dd = dt.find_next(['dd', 'td'])
            if dd:
                div_text = dd.get_text(strip=True)
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
    return {"message": "Yield Watcher API is running."}

# ルーティング設定（末尾のスラッシュの有無で弾かれないよう、2パターンを確実に明示）
@app.get("/api/stock")
@app.get("/api/stock/")
def get_stock(code: str = Query(..., description="銘柄コード")):
    code = code.strip().upper()
    clean_code = re.sub(r"\.T$", "", code)
    clean_code = re.sub(r"[^A-Z0-9]", "", clean_code)
    
    if not clean_code:
        raise HTTPException(status_code=400, detail="無効な銘柄コードです。")

    yf_code = f"{clean_code}.T"
    
    name = ""
    price = 0.0
    change = 0.0
    change_percent = 0.0
    dividend = 0.0
    dividend_yield = 0.0
    source = "scraping"
    
    scraped_price, scraped_dy, scraped_name, scraped_dividend = scrape_yahoo_finance_japan(clean_code)
    
    if scraped_price > 0.0:
        price = scraped_price
        dividend_yield = scraped_dy
        name = scraped_name
        dividend = scraped_dividend
    else:
        source = "yfinance"

    if price == 0.0 or dividend_yield == 0.0 or not name:
        try:
            ticker = yf.Ticker(yf_code)
            info = ticker.info
            if info:
                if not name:
                    name = info.get("longName") or info.get("shortName") or ""
                if price == 0.0:
                    for key in ["currentPrice", "regularMarketPrice", "previousClose", "open"]:
                        val = info.get(key)
                        if val is not None:
                            price = float(val)
                            break
                change = float(info.get("regularMarketChange") or 0.0)
                change_percent = float(info.get("regularMarketChangePercent") or 0.0)
                if dividend == 0.0:
                    dividend = float(info.get("dividendRate") or 0.0)
                if dividend_yield == 0.0:
                    dy = info.get("dividendYield")
                    if dy is not None:
                        dy_val = float(dy)
                        if dy_val == 0.0:
                            dividend_yield = 0.0
                        elif dy_val < 0.2:
                            dividend_yield = dy_val * 100
                        elif dy_val < 20.0:
                            dividend_yield = dy_val
        except Exception as e:
            logger.error(f"yfinance error: {e}")

    if not name:
        name = f"銘柄コード: {clean_code}"

    if price == 0.0:
        raise HTTPException(status_code=404, detail="データ未検出")
        
    return {
        "code": clean_code,
        "name": name,
        "price": float(price),
        "change": float(change),
        "changePercent": float(change_percent),
        "dividend": float(dividend),
        "yield": float(dividend_yield),
        "source": source
    }