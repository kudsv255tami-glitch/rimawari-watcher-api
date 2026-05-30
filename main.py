from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
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
    version="1.0.0"
)

# CORS設定（すべてのオリジンからのリクエストを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def scrape_fallback(code: str):
    """
    yfinanceでデータが取得できなかった場合のフォールバックとして、
    Yahoo!ファイナンス（日本）のHTMLをスクレイピングして株価と配当利回りを取得します。
    """
    # 銘柄コードから数字部分のみを抽出 (例: 2914.T -> 2914)
    clean_code = re.sub(r"\D", "", code)
    if not clean_code:
        return None, None
        
    url = f"https://finance.yahoo.co.jp/quote/{clean_code}.T"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        logger.info(f"Scraping Yahoo Finance Japan for code: {clean_code}")
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Failed to fetch Yahoo Finance Japan page: status {response.status_code}")
            return None, None
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        price = None
        dividend_yield = None
        
        # 1. 配当利回りの取得
        # 「配当利回り」というラベルを持つ要素を検索
        dt_tags = soup.find_all(['dt', 'th', 'td', 'span'], string=re.compile("配当利回り"))
        for dt in dt_tags:
            dd = dt.find_next(['dd', 'td'])
            if dd:
                val_text = dd.get_text(strip=True)
                # 「5.20%」や「---」などのテキストから数値を抽出
                match = re.search(r"([\d\.]+)\s*%", val_text)
                if match:
                    dividend_yield = float(match.group(1))
                    break
        
        # 2. 株価の取得
        # クラス名が動的に変わる可能性があるため、現在値が表示されやすいクラス名のパターンから検索
        price_candidates = []
        span_tags = soup.find_all('span', class_=re.compile(r'(_3rXW2W1c|value|price|kobetsu-value)'))
        for span in span_tags:
            text = span.get_text(strip=True).replace(',', '')
            # カンマを除去して、純粋な数値であるか確認
            if re.match(r'^\d+(\.\d+)?$', text):
                price_candidates.append(float(text))
                
        if price_candidates:
            price = price_candidates[0]
            
        return price, dividend_yield
    except Exception as e:
        logger.error(f"Error occurred during scraping fallback: {e}")
        return None, None

@app.get("/")
def read_root():
    return {"message": "Yield Watcher API is running. Use /api/stock?code=XXXX to fetch stock data."}

@app.get("/api/stock")
def get_stock(code: str = Query(..., description="銘柄コード (数字4桁、または末尾に.T付きのコード。例: 2914, 7203.T)")):
    """
    指定された銘柄コードの最新株価と配当利回りを取得します。
    yfinanceによるAPI取得を優先し、データ欠損時はYahoo!ファイナンスのスクレイピングを行います。
    """
    # コードの正規化
    code = code.strip().upper()
    
    # yfinance用の証券コード整形（日本株は末尾に .T が必要）
    yf_code = code
    if len(code) == 4 and code.isdigit():
        yf_code = f"{code}.T"
        
    logger.info(f"Requested code: {code} (yfinance code: {yf_code})")
    
    price = None
    dividend_yield = None
    source = "yfinance"
    
    # 1. yfinance からのデータ取得を試行
    try:
        ticker = yf.Ticker(yf_code)
        info = ticker.info
        
        if info:
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            dy = info.get("dividendYield")
            if dy is not None:
                # yfinanceは小数を返すため、100倍してパーセント表記にする (例: 0.052 -> 5.2)
                dividend_yield = float(dy) * 100
    except Exception as e:
        logger.error(f"yfinance lookup failed for {yf_code}: {e}")
        
    # 2. データのいずれかが欠損している場合、スクレイピングによるフォールバックを実行
    if price is None or dividend_yield is None:
        logger.info(f"Data incomplete from yfinance. Attempting fallback scraping for {code}...")
        scraped_price, scraped_dy = scrape_fallback(code)
        
        if price is None and scraped_price is not None:
            price = scraped_price
            source = "scraping"
        if dividend_yield is None and scraped_dy is not None:
            dividend_yield = scraped_dy
            source = "scraping"
            
    # 株価がどうしても取得できない場合はエラーとする
    if price is None:
        raise HTTPException(
            status_code=404, 
            detail=f"銘柄コード {code} の株価データを取得できませんでした。コードが正しいか確認してください。"
        )
        
    return {
        "code": code.replace(".T", ""),
        "price": price,
        "dividend_yield": dividend_yield,
        "source": source
    }
