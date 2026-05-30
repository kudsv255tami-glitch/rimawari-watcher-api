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
    version="1.1.1"
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
    Yahoo!ファイナンス（日本）のHTMLをパースして株価と配当利回りを取得します。
    """
    # 銘柄コードから数字部分のみを抽出 (例: 2914.T -> 2914)
    clean_code = re.sub(r"\D", "", code)
    if not clean_code:
        return 0.0, 0.0
        
    url = f"https://finance.yahoo.co.jp/quote/{clean_code}.T"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    price = 0.0
    dividend_yield = 0.0
    
    try:
        logger.info(f"Scraping Yahoo Finance Japan for code: {clean_code}")
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Failed to fetch Yahoo Finance Japan page: status {response.status_code}")
            return 0.0, 0.0
            
        soup = BeautifulSoup(response.text, "html.parser")
        
        # --- 1. JSON-LD (構造化データ) からの株価抽出を優先試行 ---
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

        # --- 2. HTML要素からの株価抽出 (JSON-LDで取れなかった場合) ---
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

        # --- 3. 配当利回りの取得 ---
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
                    
        return price, dividend_yield
    except Exception as e:
        logger.error(f"Error occurred during scraping fallback: {e}")
        return price, dividend_yield

@app.get("/")
def read_root():
    return {"message": "Yield Watcher API is running. Use /api/stock?code=XXXX to fetch stock data."}

@app.get("/api/stock")
def get_stock(code: str = Query(..., description="銘柄コード (数字4桁、または末尾に.T付きのコード。例: 2914, 7203.T)")):
    """
    指定された銘柄コードの最新株価と配当利回りを取得します。
    yfinanceによる取得を優先し、データ欠損時や異常値検出時はYahoo!ファイナンス(日本)のスクレイピングを行います。
    """
    # コードの正規化
    code = code.strip().upper()
    
    # yfinance用の証券コード整形（日本株は末尾に .T が必要）
    yf_code = code
    if len(code) == 4 and code.isdigit():
        yf_code = f"{code}.T"
        
    logger.info(f"Requested code: {code} (yfinance code: {yf_code})")
    
    price = 0.0
    dividend_yield = 0.0
    source = "yfinance"
    
    yfinance_success = False
    
    # 1. yfinance からのデータ取得を試行
    try:
        ticker = yf.Ticker(yf_code)
        info = ticker.info
        
        if info:
            # 株価の確実な取得（複数のキーを優先度順に確認）
            for key in ["currentPrice", "regularMarketPrice", "previousClose", "open"]:
                val = info.get(key)
                if val is not None:
                    try:
                        price = float(val)
                        break
                    except ValueError:
                        continue
            
            # 配当利回りの取得とクレンジング
            dy = info.get("dividendYield")
            if dy is not None:
                try:
                    dy_val = float(dy)
                    
                    # 単位・異常値の自動判定ロジック:
                    # yfinanceが返却する値が小数表記、パーセント表記、配当金などの数値のいずれであるかを判定し
                    # 一貫して「3.92」のようなパーセンテージ表記(float)に統一します。
                    if dy_val == 0.0:
                        dividend_yield = 0.0
                    # A. 小数表記の場合 (例: 0.0392 -> 3.92%)
                    # 日本株で配当利回りが 20% (0.2) を超えることはほぼないため、0.2未満は小数とみなします。
                    elif dy_val < 0.2:
                        dividend_yield = dy_val * 100
                    # B. すでにパーセント表記の場合 (例: 3.92 -> 3.92%)
                    # 20%未満であれば、そのままパーセント表記として採用します。
                    elif dy_val < 20.0:
                        dividend_yield = dy_val
                    # C. 異常に大きな値の場合 (例: 192 や 392 など、配当金額そのものが混入している可能性)
                    # 100で割った値（3.92など）が 20% 未満に収まる場合は、その値（100で割った結果）を配当利回りとして採用します。
                    else:
                        divided_val = dy_val / 100
                        if divided_val < 20.0:
                            dividend_yield = divided_val
                        else:
                            # それでも異常な場合は 0.0 にしてスクレイピングにフォールバックさせます。
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
        scraped_price, scraped_dy = scrape_fallback(code)
        
        # 株価の更新: スクレイピングで取れた最新の日本市場株価を優先
        if scraped_price > 0.0:
            price = scraped_price
            source = "scraping"
            
        # 配当利回りの更新
        if dividend_yield == 0.0 and scraped_dy > 0.0:
            dividend_yield = scraped_dy
            source = "scraping"
            
    # 株価がどうしても取得できなかった場合はエラーとする
    if price == 0.0:
        raise HTTPException(
            status_code=404, 
            detail=f"銘柄コード {code} の株価データを取得できませんでした。コードが正しいか確認してください。"
        )
        
    # float型であることを保証してJSONを返却
    return {
        "code": code.replace(".T", ""),
        "price": float(price),
        "dividend_yield": float(dividend_yield),
        "source": source
    }
