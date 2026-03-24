import pandas as pd
import numpy as np
import os
import glob
from colorama import init, Fore, Style
import twstock
from datetime import datetime, timedelta

# ==========================================
# 1. 參數設定 (嚴格參照 test.py CONFIG)
# ==========================================

# 路徑設定
CHIP_FOLDER = r'C:\Users\b3134\Desktop\stock_chip_data\stock_chip_data'
PRICE_FOLDER = r'C:\Users\b3134\Desktop\stock_chip_data\daily_price_data'
RESULT_FILE = 'weekly_buy_candidates_v3.csv' # 輸出檔名

# --- 大盤熔斷機制 ---
BENCHMARK_FILE = '0000.csv'
CRASH_DROP_THRESHOLD = 0.04   # 單日跌幅 > 4%
CRASH_CHILL_DAYS = 35         # 冷靜期 35 天

# --- 基礎濾網 ---
MIN_PRICE = 10.0
MIN_WEEKLY_VOL = 5000          # 修正：依照 test.py 改為 5000 張
ABNORMAL_JUMP_THRESHOLD = 20.0 # 排除單週暴增 > 20%
VOL_MOMENTUM_RATIO = 1.2       # 修正：本週量需大於上週量 1.2 倍

# --- 三級距策略參數 (Tiered Strategy) ---
# Tier 1: 小型股 (< 1500億)
T1_CAP_LIMIT = 1500.0 
T1_1W_TH = 3.0    # 單周 3%
T1_2W_TH = 5.0    # 雙周 5%
T1_WEIGHT = 2.0   # 權重

# Tier 2: 中型股 (1500億 ~ 4000億)
T2_CAP_LIMIT = 4000.0
T2_1W_TH = 1.7    # 單周 1.7%
T2_2W_TH = 3.0    # 雙周 3%
T2_WEIGHT = 1.0   # 權重

# Tier 3: 大型股 (> 4000億)
T3_1W_TH = 0.2    # 單周 0.2%
T3_2W_TH = 0.5    # 雙周 0.5%
T3_WEIGHT = 1.3   # 權重

init(autoreset=True)

# ================= 輔助函式 =================
def get_stock_name(stock_id):
    """取得股票中文名稱"""
    try:
        if stock_id in twstock.codes:
            return twstock.codes[stock_id].name
    except:
        pass
    return stock_id

def calculate_ma(series, window):
    return series.rolling(window=window).mean()

def is_market_crashing():
    """檢查大盤是否熔斷"""
    try:
        bm_path = os.path.join(PRICE_FOLDER, BENCHMARK_FILE)
        if not os.path.exists(bm_path):
            print(f"{Fore.YELLOW}⚠️ 警告：找不到大盤資料 {BENCHMARK_FILE}，跳過熔斷檢查。")
            return False
            
        df_bm = pd.read_csv(bm_path)
        df_bm['Date'] = pd.to_datetime(df_bm['Date'])
        df_bm = df_bm.sort_values('Date')
        
        # 計算前日收盤以計算跌幅
        df_bm['Prev_Close'] = df_bm['Close'].shift(1)
        df_bm['Drop_Rate'] = (df_bm['Prev_Close'] - df_bm['Close']) / df_bm['Prev_Close']
        
        # 找出崩跌日
        crash_days = df_bm[df_bm['Drop_Rate'] >= CRASH_DROP_THRESHOLD]
        
        if crash_days.empty:
            return False
            
        last_crash_date = crash_days.iloc[-1]['Date']
        last_data_date = df_bm.iloc[-1]['Date']
        
        days_since_crash = (last_data_date - last_crash_date).days
        
        if days_since_crash < CRASH_CHILL_DAYS:
            print(f"{Fore.RED}⛔ 市場處於熔斷冷靜期！(距上次大跌僅 {days_since_crash} 天)")
            return True
            
        return False
        
    except Exception as e:
        print(f"{Fore.RED}❌ 熔斷檢查發生錯誤: {e}")
        return False

# ================= 主程式 =================
def main():
    print(f"{Fore.CYAN}🔍 開始掃描買進訊號 (V3 Tiered Strategy - 完全同步回測版)...")
    
    if is_market_crashing():
        print(f"{Fore.RED}⛔ 系統依據策略暫停買進 (大盤熔斷中)。")
        return

    candidates = []
    
    # 取得所有籌碼檔案
    chip_files = glob.glob(os.path.join(CHIP_FOLDER, "*.csv"))
    print(f"📂 找到 {len(chip_files)} 檔籌碼資料，開始分析...")

    for i, file_path in enumerate(chip_files):
        filename = os.path.basename(file_path)
        stock_id = filename.split('.')[0]
        
        if i % 100 == 0:
            print(f"\rProcessing... {i}/{len(chip_files)}", end="")

        try:
            # -------------------------------------------------------
            # 1. 讀取與處理籌碼資料
            # -------------------------------------------------------
            df_chip = pd.read_csv(file_path)
            
            # 標準化欄位
            if '資料日期' in df_chip.columns:
                df_chip['Date'] = pd.to_datetime(df_chip['資料日期'].astype(str), format='%Y%m%d', errors='coerce')
            elif 'Date' in df_chip.columns:
                df_chip['Date'] = pd.to_datetime(df_chip['Date'], errors='coerce')
            else:
                continue

            # 處理數值 (移除逗號轉 float)
            for col in ['集保總張數', '>400張大股東持有百分比']:
                if col in df_chip.columns and df_chip[col].dtype == object:
                    df_chip[col] = df_chip[col].astype(str).str.replace(',', '').astype(float)
            
            df_chip = df_chip.dropna(subset=['Date']).sort_values('Date')
            
            if len(df_chip) < 3:
                continue

            # 取得最新一筆 (本週)、上一筆 (上週)、上上筆 (上上週)
            # 注意：這裡假設資料是每週一筆。若為日更籌碼需自行 resampling，但通常集保是週更。
            row_latest = df_chip.iloc[-1]
            row_prev_1 = df_chip.iloc[-2]
            row_prev_2 = df_chip.iloc[-3]
            
            report_date = row_latest['Date']
            prev_date = row_prev_1['Date']

            # -------------------------------------------------------
            # 2. 讀取與處理股價資料 (計算 MA20 與成交量)
            # -------------------------------------------------------
            price_file = os.path.join(PRICE_FOLDER, f"{stock_id}.csv")
            if not os.path.exists(price_file):
                price_file = os.path.join(PRICE_FOLDER, f"{stock_id}.TW.csv")
                if not os.path.exists(price_file):
                    continue
            
            df_price = pd.read_csv(price_file)
            df_price['Date'] = pd.to_datetime(df_price['Date'])
            df_price = df_price.sort_values('Date')
            
            # 計算 MA20
            df_price['MA20'] = df_price['Close'].rolling(window=20).mean()
            
            # 取得對應籌碼日期的股價資訊
            # 這裡使用 asof 找最接近籌碼日期的股價 (通常是週五)
            try:
                price_idx = df_price[df_price['Date'] <= report_date].index[-1]
                curr_price_row = df_price.loc[price_idx]
            except IndexError:
                continue # 找不到對應日期股價
            
            close_price = float(curr_price_row['Close'])
            ma20 = float(curr_price_row['MA20'])
            
            # --- 濾網 A: 股價必須 > 10元 ---
            if close_price < MIN_PRICE:
                continue

            # --- 濾網 B: 股價必須在月線 (MA20) 之上 (test.py 邏輯) ---
            if close_price <= ma20:
                continue
                
            # --- 濾網 C: 成交量檢查 ---
            # 計算本週總成交量 (籌碼日期往前推7天)
            mask_this_week = (df_price['Date'] > (report_date - timedelta(days=7))) & (df_price['Date'] <= report_date)
            vol_this_week = df_price.loc[mask_this_week, 'Volume'].sum()
            
            # 計算上週總成交量
            mask_last_week = (df_price['Date'] > (prev_date - timedelta(days=7))) & (df_price['Date'] <= prev_date)
            vol_last_week = df_price.loc[mask_last_week, 'Volume'].sum()
            
            # C-1: 週量門檻
            if vol_this_week/1000 < MIN_WEEKLY_VOL:
                continue
                
            # C-2: 量能爆發 (本週 > 上週 * 1.2)
            if vol_last_week == 0: continue
            if vol_this_week <= vol_last_week * VOL_MOMENTUM_RATIO:
                continue

            # -------------------------------------------------------
            # 3. 籌碼邏輯運算
            # -------------------------------------------------------
            
            # 市值計算 (億)
            total_sheets = float(row_latest['集保總張數'])
            market_cap = (close_price * total_sheets * 1000) / 100000000 # 注意單位換算：張->股(/1000)
            
            # 籌碼變動
            share_latest = float(row_latest['>400張大股東持有百分比'])
            share_prev_1 = float(row_prev_1['>400張大股東持有百分比'])
            share_prev_2 = float(row_prev_2['>400張大股東持有百分比'])
            
            diff_1w = share_latest - share_prev_1
            diff_2w = share_latest - share_prev_2
            
            # 異常暴衝檢查
            if diff_1w > ABNORMAL_JUMP_THRESHOLD:
                continue

            # -------------------------------------------------------
            # 4. 分級評分策略 (完全依照 test.py)
            # -------------------------------------------------------
            
            final_score = 0
            tier = 0
            raw_val = 0 # 紀錄是用 1W 還是 2W 進場的原始數值
            trigger_msg = ""
            
            # 設定門檻
            th_1w, th_2w, weight = 0, 0, 0
            
            if market_cap < T1_CAP_LIMIT: # Tier 1
                tier = 1
                th_1w, th_2w, weight = T1_1W_TH, T1_2W_TH, T1_WEIGHT
            elif market_cap < T2_CAP_LIMIT: # Tier 2
                tier = 2
                th_1w, th_2w, weight = T2_1W_TH, T2_2W_TH, T2_WEIGHT
            else: # Tier 3
                tier = 3
                th_1w, th_2w, weight = T3_1W_TH, T3_2W_TH, T3_WEIGHT

            # 計算分數： (Diff / Threshold) * Weight * 100
            
            # 檢查單週
            if diff_1w >= th_1w:
                score_1w = (diff_1w / th_1w) * weight * 100
                if score_1w > final_score:
                    final_score = score_1w
                    raw_val = diff_1w
                    trigger_msg = f"T{tier}_1W({diff_1w:.2f}%)"
            
            # 檢查雙週
            if diff_2w >= th_2w:
                score_2w = (diff_2w / th_2w) * weight * 100
                if score_2w > final_score:
                    final_score = score_2w
                    raw_val = diff_2w
                    trigger_msg = f"T{tier}_2W({diff_2w:.2f}%)"

            # -------------------------------------------------------
            # 5. 加入候選名單
            # -------------------------------------------------------
            if final_score > 0:
                candidates.append({
                    'Stock': stock_id,
                    'Name': get_stock_name(stock_id),
                    'Close': close_price,
                    'Tier': tier,
                    'Score': round(final_score, 2),
                    'Chip_Diff_1W': round(diff_1w, 2),
                    'Chip_Diff_2W': round(diff_2w, 2),
                    'Trigger': trigger_msg,
                    'MarketCap(E)': int(market_cap),
                    'MA20': round(ma20, 2),
                    'Vol_ThisW': int(vol_this_week/1000),
                    'Vol_LastW': int(vol_last_week/1000)
                })

        except Exception as e:
            # print(f"Error processing {stock_id}: {e}")
            continue

    # -------------------------------------------------------
    # 輸出結果
    # -------------------------------------------------------
    if candidates:
        df_out = pd.DataFrame(candidates)
        # 依照 Score 由高到低排序
        df_out = df_out.sort_values('Score', ascending=False)
        
        # 調整欄位順序
        cols = ['Stock', 'Name', 'Close', 'Score', 'Trigger', 'Tier', 
                'Chip_Diff_1W', 'Chip_Diff_2W', 'MarketCap(E)', 'MA20', 'Vol_ThisW', 'Vol_LastW']
        # 確保欄位存在
        df_out = df_out[[c for c in cols if c in df_out.columns]]

        # 存檔
        df_out.to_csv(RESULT_FILE, index=False, encoding='utf-8-sig')
        
        print(f"\n{Fore.GREEN}{'='*100}")
        print(f"🎯 掃描完成！共發現 {len(df_out)} 檔標的")
        print(f"策略邏輯: Price>MA20 & Vol>5000 & VolMom>1.2 & TieredChip")
        print(f"檔案已儲存至: {RESULT_FILE}")
        print(f"{'='*100}{Style.RESET_ALL}")
        
        # 顯示前 30 筆
        print(df_out.head(30).to_string(index=False))
        
    else:
        print(f"\n{Fore.YELLOW}🐢 本週無符合條件標的。")

if __name__ == "__main__":
    main()