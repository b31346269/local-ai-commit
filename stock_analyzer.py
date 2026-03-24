import pandas as pd
import numpy as np
import os
import glob
import warnings
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# 忽略 pandas 警告
warnings.filterwarnings('ignore')

# 設定中文字型
plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei'] 
plt.rcParams['axes.unicode_minus'] = False

# ==========================================
# 1. 策略參數設定 (完全對齊 test.py)
# ==========================================
CONFIG = {
    # 時間設定 (從 2022/9 開始)
    'START_DATE': '2017/6/5',

    # 資料路徑 (保留 test_strategy_pro 的路徑)
    'DATA_DIR': r'C:\Users\b3134\Desktop\processed_data',
    
    # 資金管理
    'INITIAL_CAPITAL': 1_000_000, 
    'MAX_POSITIONS': 15,          
    'FEE_RATE': 0.001425,       
    'TAX_RATE': 0.003,          
    'SLIPPAGE': 0.003, 

    # 基礎停損停利 (會被分級停損覆蓋)
    'HARD_STOP_LOSS': 0.20,     
    'TRAILING_STOP': 0.15,      
    
    # 大盤熔斷
    'BENCHMARK_DROP_THRESHOLD': 0.04, 
    'CRASH_FREEZE_DAYS': 35,          

    # 進場濾網
    'MIN_WEEKLY_VOL': 5000,           
    'ABNORMAL_JUMP_THRESHOLD': 20.0, 

    # --- Tiered Strategy (三級距) ---
    # [Tier 1] < 1500億
    'T1_CAP_LIMIT': 1500,
    'T1_1W_TH': 30.0,    
    'T1_2W_TH': 50.0,    
    'T1_WEIGHT': 2.0,   
    # [Tier 2] 1500~4000億
    'T2_CAP_LIMIT': 4000,
    'T2_1W_TH': 1.7,    
    'T2_2W_TH': 3.0,    
    'T2_WEIGHT': 1.0,   
    # [Tier 3] > 4000億
    'T3_1W_TH': 0.2,    
    'T3_2W_TH': 0.55,    
    'T3_WEIGHT': 1.3    
}

# ==========================================
# 2. 資料處理 (適配 processed_data 格式)
# ==========================================
class DataHandler:
    def __init__(self):
        self.stock_data = {} # 暫存原始資料
        self.price_data = {} # 符合 test.py 格式的價格資料
        self.chip_data = {}  # 符合 test.py 格式的籌碼資料
        self.benchmark_data = None
        
    def load_data(self):
        print("正在讀取資料...")
        
        # 1. 讀取大盤
        benchmark_path = os.path.join(CONFIG['DATA_DIR'], '0000.csv')
        if not os.path.exists(benchmark_path):
            print("錯誤：找不到大盤資料 0000.csv")
            return
            
        bench_df = pd.read_csv(benchmark_path, index_col='Date', parse_dates=True)
        bench_df = bench_df.sort_index()
        # 計算大盤跌幅 (用於熔斷)
        bench_df['Prev_Close'] = bench_df['Close'].shift(1)
        bench_df['Drop_Rate'] = (bench_df['Prev_Close'] - bench_df['Close']) / bench_df['Prev_Close']
        self.benchmark_data = bench_df
        
        # 2. 讀取個股 (遍歷 processed_data)
        all_files = glob.glob(os.path.join(CONFIG['DATA_DIR'], "*.csv"))
        all_files = [f for f in all_files if "0000.csv" not in f]
        
        start_dt = pd.to_datetime(CONFIG['START_DATE'])
        
        for f in all_files:
            stock_id = os.path.basename(f).replace('.csv', '')
            try:
                # 讀取合併後的檔案 (包含 Price 和 Chip)
                df = pd.read_csv(f, index_col='Date', parse_dates=True)
                df = df.sort_index()
                
                # 欄位檢查與對應
                # 來源欄位: Open, High, Low, Close, Volume, Major_Hold_Pct, Total_Shares
                if 'Major_Hold_Pct' not in df.columns: continue
                
                # --- 建構 Price Data (test.py 格式) ---
                pdf = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
                # 計算 MA
                pdf['MA5'] = pdf['Close'].rolling(window=5).mean()
                pdf['MA20'] = pdf['Close'].rolling(window=20).mean()
                
                # 過濾日期
                pdf = pdf[pdf.index >= start_dt - timedelta(days=60)]
                self.price_data[stock_id] = pdf
                
                # --- 建構 Chip Data (test.py 格式) ---
                cdf = df[['Major_Hold_Pct', 'Total_Shares']].copy()
                # 欄位更名以符合 test.py 邏輯
                cdf = cdf.rename(columns={
                    'Major_Hold_Pct': '>400張大股東持有百分比',
                    'Total_Shares': '集保總張數'
                })
                
                # 過濾日期
                cdf = cdf[cdf.index >= start_dt - timedelta(days=60)]
                self.chip_data[stock_id] = cdf
                    
            except Exception as e:
                # print(f"Load error {stock_id}: {e}")
                continue
                
        print(f"成功載入 {len(self.price_data)} 檔股票。")

# ==========================================
# 3. 回測引擎 (邏輯與 test.py 一致)
# ==========================================
class BacktestEngine:
    def __init__(self, data_handler):
        self.dh = data_handler
        self.cash = CONFIG['INITIAL_CAPITAL']
        self.positions = {} 
        self.history = []
        self.trade_records = [] 
        
        self.crash_protection_days = 0
        self.order_queue = [] # 待執行訂單
        
        self.buy_stats = []
        self.sell_stats = []

    def get_price(self, stock_id, date):
        if stock_id not in self.dh.price_data: return None
        df = self.dh.price_data[stock_id]
        idx = df.index.asof(date)
        if pd.isna(idx): return None
        if (date - idx).days > 5: return None
        return df.loc[idx]

    def get_weekly_volume(self, stock_id, end_date):
        if stock_id not in self.dh.price_data: return 0
        df = self.dh.price_data[stock_id]
        start_date = end_date - timedelta(days=6)
        mask = (df.index >= start_date) & (df.index <= end_date)
        return df.loc[mask, 'Volume'].sum()

    def execute_orders(self, date):
        if not self.order_queue: return
        
        sell_orders = [o for o in self.order_queue if o['action'] == 'SELL']
        buy_orders = [o for o in self.order_queue if o['action'] == 'BUY']
        
        # 1. 執行賣出
        for order in sell_orders:
            sid = order['stock_id']
            row = self.get_price(sid, date)
            if row is None: continue 
            
            open_price = row['Open']
            self._sell_stock(sid, open_price, date, order['reason'], order['signal_val'])
        
        # 2. 執行買入
        if self.crash_protection_days > 0:
            pass # 熔斷期間不買入
        else:
            # 依據 signal_val (分數) 排序：大到小
            buy_orders.sort(key=lambda x: x['signal_val'], reverse=True)
            
            for order in buy_orders:
                sid = order['stock_id']
                row = self.get_price(sid, date)
                if row is None: continue
                
                open_price = row['Open']
                if sid in self.positions: continue
                
                stats_val = order.get('stats_val', 0)
                self._buy_stock(sid, open_price, date, order['reason'], order['signal_val'], stats_val)
            
        self.order_queue = []

    def _buy_stock(self, stock_id, price, date, reason, signal_val, stats_val=None):
        if len(self.positions) >= CONFIG['MAX_POSITIONS']: return

        equity = self.calculate_equity(date)
        target_amt = equity / CONFIG['MAX_POSITIONS']
        invest_amt = min(self.cash, target_amt)
        
        if invest_amt < 10000: return
        
        exec_price = price * (1 + CONFIG['SLIPPAGE']) 
        cost_per_share = exec_price * (1 + CONFIG['FEE_RATE'])
        shares = int(invest_amt // cost_per_share)
        
        if shares > 0:
            total_cost = shares * cost_per_share
            self.cash -= total_cost
            
            # --- 計算進場市值 ---
            entry_mcap = 0
            if stock_id in self.dh.chip_data:
                cdf = self.dh.chip_data[stock_id]
                idx = cdf.index.asof(date)
                if not pd.isna(idx):
                    total_sheets = cdf.loc[idx]['集保總張數']
                    # 市值(億) = 股價 * 股本(張)*1000 / 1億
                    entry_mcap = (exec_price * total_sheets * 1000) / 100_000_000

            display_val = stats_val if stats_val is not None else signal_val
            
            self.positions[stock_id] = {
                'shares': shares,
                'cost_price': exec_price,
                'max_price': exec_price,
                'entry_date': date,
                'buy_reason': f"{reason}({display_val:.1f}%)", 
                'buy_signal': signal_val,
                'entry_mcap': entry_mcap 
            }
            self.buy_stats.append(display_val)

    def _sell_stock(self, stock_id, price, date, reason, signal_val):
        if stock_id not in self.positions: return
        
        pos = self.positions[stock_id]
        shares = pos['shares']
        
        exec_price = price * (1 - CONFIG['SLIPPAGE']) 
        
        revenue = shares * exec_price
        fee = revenue * CONFIG['FEE_RATE']
        tax = revenue * CONFIG['TAX_RATE']
        net_payout = revenue - fee - tax
        
        self.cash += net_payout
        
        cost_per_share = pos['cost_price'] * (1 + CONFIG['FEE_RATE'])
        initial_cost = shares * cost_per_share
        profit_amt = net_payout - initial_cost
        roi = profit_amt / initial_cost
        
        self.trade_records.append({
            'Stock': stock_id,
            'Market_Cap': round(pos.get('entry_mcap', 0), 1),
            'Buy_Date': pos['entry_date'].strftime('%Y-%m-%d'),
            'Buy_Price': pos['cost_price'],
            'Buy_Reason': pos['buy_reason'],
            'Sell_Date': date.strftime('%Y-%m-%d'),
            'Sell_Price': exec_price,
            'Sell_Reason': reason,
            'Hold_Days': (date - pos['entry_date']).days,
            'Return_Pct': roi,
            'Profit_Amt': profit_amt,
            'Sell_Signal_Val': signal_val
        })
        
        if '籌碼' in reason:
            self.sell_stats.append(signal_val)
            
        del self.positions[stock_id]

    def calculate_equity(self, date):
        equity = self.cash
        for sid, pos in self.positions.items():
            row = self.get_price(sid, date)
            p = row['Close'] if row is not None else pos['max_price']
            equity += pos['shares'] * p
        return equity

    def run(self):
        start_dt = pd.to_datetime(CONFIG['START_DATE'])
        # 使用大盤日期作為回測時間軸
        all_dates = self.dh.benchmark_data.index.unique()
        dates = sorted([d for d in all_dates if d >= start_dt])
        
        if not dates: return pd.DataFrame()
        
        print(f"開始回測 V3_Tiered_Strategy ({dates[0].date()} ~ {dates[-1].date()})...")
        
        for current_date in dates:
            
            # --- 1. 開盤階段 ---
            self.execute_orders(current_date)
            
            # --- 2. 盤中/收盤檢查 ---
            # 檢查大盤熔斷
            if current_date in self.dh.benchmark_data.index:
                bench_row = self.dh.benchmark_data.loc[current_date]
                if bench_row['Drop_Rate'] >= CONFIG['BENCHMARK_DROP_THRESHOLD']:
                    print(f"⚠️ {current_date.date()} 大盤崩跌 {bench_row['Drop_Rate']*100:.2f}%! 啟動熔斷保護 {CONFIG['CRASH_FREEZE_DAYS']} 天")
                    self.crash_protection_days = CONFIG['CRASH_FREEZE_DAYS']

            if self.crash_protection_days > 0:
                self.crash_protection_days -= 1

            # 持股停損停利
            for sid, pos in self.positions.items():
                row = self.get_price(sid, current_date)
                if row is None: continue
                
                close_price = row['Close']
                high_price = row['High']
                
                # 更新最高價
                if high_price > pos['max_price']:
                    self.positions[sid]['max_price'] = high_price
                
                loss_rate = (close_price - pos['cost_price']) / pos['cost_price']
                
                if any(o['stock_id'] == sid and o['action'] == 'SELL' for o in self.order_queue): continue

                # 分級停損邏輯
                entry_mcap = pos.get('entry_mcap', 0) 
                current_stop = 0.15 

                if entry_mcap < 1500:       # < 1500億
                    current_stop = 0.20     
                elif entry_mcap > 4000:     # > 4000億
                    current_stop = 0.10     
                else:
                    current_stop = 0.15     

                # 檢查硬性停損
                if loss_rate <= -current_stop:
                    self.order_queue.append({
                        'stock_id': sid, 'action': 'SELL', 
                        'reason': f'硬性停損({current_stop*100:.0f}%)',
                        'signal_val': 0
                    })
                    continue
                
                # 檢查移動停利
                drawdown = (close_price - pos['max_price']) / pos['max_price']
                if drawdown <= -CONFIG['TRAILING_STOP']:
                    self.order_queue.append({
                        'stock_id': sid, 'action': 'SELL', 
                        'reason': '移動停利', 'signal_val': 0
                    })
                    continue

            # --- 3. 週末/策略掃描 ---
            if current_date.weekday() == 4: # 週五
                self.run_weekend_analysis(current_date)

            # --- 紀錄權益 ---
            eq = self.calculate_equity(current_date)
            bm_close = self.dh.benchmark_data.loc[current_date]['Close']
            
            self.history.append({
                'Date': current_date,
                'Equity': eq,
                'Benchmark': bm_close,
                'Positions': len(self.positions),
                'Is_Freezing': 1 if self.crash_protection_days > 0 else 0
            })

        return pd.DataFrame(self.history)

    def run_weekend_analysis(self, report_date):
        """每週五收盤後執行"""
        
        # 1. 籌碼賣訊檢查
        for sid in list(self.positions.keys()):
            if any(o['stock_id'] == sid and o['action'] == 'SELL' for o in self.order_queue): continue
            
            should_sell, drop_val = self.check_chip_sell_signal(sid, report_date)
            if should_sell:
                self.order_queue.append({
                    'stock_id': sid, 'action': 'SELL',
                    'reason': '籌碼鬆動且破5日', 'signal_val': drop_val
                })

        # 2. 買進掃描
        candidates = self.scan_candidates(report_date)
        for sid, score, raw_val in candidates:
            if sid in self.positions: continue
            if any(o['stock_id'] == sid and o['action'] == 'BUY' for o in self.order_queue): continue
            
            self.order_queue.append({
                'stock_id': sid, 'action': 'BUY',
                'reason': '週選', 
                'signal_val': score,    
                'stats_val': raw_val    
            })

    def scan_candidates(self, report_date):
        candidates = [] 
        for sid, chip_df in self.dh.chip_data.items():
            idx = chip_df.index.asof(report_date)
            if pd.isna(idx): continue
            curr_chip = chip_df.loc[idx]
            
            if (report_date - idx).days > 7: continue 
            
            prev_idx = chip_df.index.asof(idx - timedelta(days=1))
            if pd.isna(prev_idx): continue
            prev_chip = chip_df.loc[prev_idx]
            
            prev2_idx = chip_df.index.asof(prev_idx - timedelta(days=1))
            has_prev2 = not pd.isna(prev2_idx)
            prev2_chip = chip_df.loc[prev2_idx] if has_prev2 else None

            price_row = self.get_price(sid, report_date) 
            if price_row is None: continue
            
            # 濾網：股價必須在 20MA (月線) 之上
            if price_row['Close'] <= price_row['MA20']: continue 
            
            # 市值計算 (單位: 億)
            market_cap = (price_row['Close'] * curr_chip['集保總張數']*1000) / 100000000
            
            # --- 成交量濾網 ---
            vol_this_week = self.get_weekly_volume(sid, report_date)
            if vol_this_week < CONFIG['MIN_WEEKLY_VOL']: continue 

            vol_last_week = self.get_weekly_volume(sid, prev_idx)
            if vol_last_week == 0: continue
            if vol_this_week <= vol_last_week * 1.2: continue 
                
            # 籌碼計算
            diff_1w = curr_chip['>400張大股東持有百分比'] - prev_chip['>400張大股東持有百分比']
            if diff_1w > CONFIG['ABNORMAL_JUMP_THRESHOLD']: continue 

            diff_2w = 0
            if has_prev2:
                diff_2w = curr_chip['>400張大股東持有百分比'] - prev2_chip['>400張大股東持有百分比']
            
            # 三級距分層
            final_score = 0
            raw_val = 0
            
            th_1w, th_2w, weight = 0, 0, 0
            
            if market_cap < CONFIG['T1_CAP_LIMIT']: # < 1500億
                th_1w = CONFIG['T1_1W_TH']
                th_2w = CONFIG['T1_2W_TH']
                weight = CONFIG['T1_WEIGHT']
                
            elif market_cap < CONFIG['T2_CAP_LIMIT']: # 1500 ~ 4000億
                th_1w = CONFIG['T2_1W_TH']
                th_2w = CONFIG['T2_2W_TH']
                weight = CONFIG['T2_WEIGHT']
                
            else: # > 4000億
                th_1w = CONFIG['T3_1W_TH']
                th_2w = CONFIG['T3_2W_TH']
                weight = CONFIG['T3_WEIGHT']

            if diff_1w >= th_1w:
                score_1w = (diff_1w / th_1w) * weight * 100
                if score_1w > final_score:
                    final_score = score_1w
                    raw_val = diff_1w
            
            if has_prev2 and diff_2w >= th_2w:
                score_2w = (diff_2w / th_2w) * weight * 100
                if score_2w > final_score:
                    final_score = score_2w
                    raw_val = diff_2w

            if final_score > 0:
                candidates.append((sid, final_score, raw_val))
                
        return candidates

    def check_chip_sell_signal(self, sid, report_date):
        if sid not in self.dh.chip_data: return False, 0
        chip_df = self.dh.chip_data[sid]
        
        idx = chip_df.index.asof(report_date)
        if pd.isna(idx): return False, 0
        curr_chip = chip_df.loc[idx]
        if (report_date - idx).days > 7: return False, 0
        
        prev_idx = chip_df.index.asof(idx - timedelta(days=1))
        if pd.isna(prev_idx): return False, 0
        prev_chip = chip_df.loc[prev_idx]
        
        prev2_idx = chip_df.index.asof(prev_idx - timedelta(days=1))
        has_prev2 = not pd.isna(prev2_idx)
        prev2_chip = chip_df.loc[prev2_idx] if has_prev2 else None
        
        price_row = self.get_price(sid, report_date)
        if price_row is None: return False, 0
        
        market_cap = (price_row['Close'] * curr_chip['集保總張數']*1000) / 100000000
        
        drop_1w = -(curr_chip['>400張大股東持有百分比'] - prev_chip['>400張大股東持有百分比'])
        drop_2w = 0
        if has_prev2:
            drop_2w = -(curr_chip['>400張大股東持有百分比'] - prev2_chip['>400張大股東持有百分比'])
            
        limit_1w = 0
        limit_2w = 0
        
        if market_cap < CONFIG['T1_CAP_LIMIT']:
            limit_1w = CONFIG['T1_1W_TH']
            limit_2w = CONFIG['T1_2W_TH']
        elif market_cap < CONFIG['T2_CAP_LIMIT']:
            limit_1w = CONFIG['T2_1W_TH']
            limit_2w = CONFIG['T2_2W_TH']
        else:
            limit_1w = CONFIG['T3_1W_TH']
            limit_2w = CONFIG['T3_2W_TH']

        # MA5 護盾檢查
        if price_row['Close'] >= price_row['MA5']:
            if max(drop_1w, drop_2w) < (limit_1w * 1.5):
                return False, 0 
        
        is_sell = False
        max_drop = max(drop_1w, drop_2w)
        
        if drop_1w >= limit_1w or drop_2w >= limit_2w:
            is_sell = True
            
        return is_sell, max_drop

# ==========================================
# 4. 分析與輸出 (保留 test.py 統計功能)
# ==========================================
def analyze_results(df, engine):
    df = df.set_index('Date')
    df.to_csv(os.path.join(CONFIG['DATA_DIR'], 'daily_equity.csv'))
    
    trade_df = pd.DataFrame(engine.trade_records)
    if not trade_df.empty:
        trade_df['Return_Pct_Str'] = (trade_df['Return_Pct'] * 100).map('{:,.2f}%'.format)
        
        cols = ['Stock', 'Market_Cap', 'Buy_Date', 'Buy_Price', 'Buy_Reason', 
                'Sell_Date', 'Sell_Price', 'Sell_Reason', 
                'Hold_Days', 'Return_Pct', 'Profit_Amt', 'Sell_Signal_Val']
        
        valid_cols = [c for c in cols if c in trade_df.columns]
        trade_df = trade_df[valid_cols]
        
        trade_df.to_csv(os.path.join(CONFIG['DATA_DIR'], 'trade_details.csv'), index=False, encoding='utf-8-sig')
    
    total_ret = (df['Equity'].iloc[-1] / CONFIG['INITIAL_CAPITAL']) - 1
    
    df['Strat_Ret'] = df['Equity'].pct_change().fillna(0)
    df['Bench_Ret'] = df['Benchmark'].pct_change().fillna(0)
    
    cov = np.cov(df['Strat_Ret'], df['Bench_Ret'])
    beta = cov[0, 1] / np.var(df['Bench_Ret'])
    
    days = (df.index[-1] - df.index[0]).days
    cagr_strat = (df['Equity'].iloc[-1] / CONFIG['INITIAL_CAPITAL']) ** (365/days) - 1
    cagr_bench = (df['Benchmark'].iloc[-1] / df['Benchmark'].iloc[0]) ** (365/days) - 1
    
    alpha = cagr_strat - (0.015 + beta * (cagr_bench - 0.015))
    sharpe = (cagr_strat - 0.015) / (df['Strat_Ret'].std() * np.sqrt(252))
    
    df['Peak'] = df['Equity'].cummax()
    df['Drawdown'] = (df['Equity'] - df['Peak']) / df['Peak']
    max_dd = df['Drawdown'].min()
    
    print("\n" + "="*50)
    print(f"🏁 回測結束 V3_Tiered_Strategy")
    print("="*50)
    print(f"初始資金: {CONFIG['INITIAL_CAPITAL']:,}")
    print(f"最終權益: {int(df['Equity'].iloc[-1]):,} (NTD)")
    print(f"總報酬率: {total_ret*100:.2f}%")
    print(f"交易筆數: {len(trade_df)} 筆")
    print("-" * 50)
    print(f"年化報酬 (CAGR): {cagr_strat*100:.2f}%")
    print(f"大盤年化 (CAGR): {cagr_bench*100:.2f}%")
    print(f"Alpha (α):       {alpha*100:.2f}%")
    print(f"Beta  (β):       {beta:.2f}")
    print(f"Sharpe Ratio:    {sharpe:.2f}")
    print(f"Max Drawdown:    {max_dd*100:.2f}%")
    print("="*50)

    return df, trade_df

def print_stats(engine):
    print("\n📊 訊號級距統計 (1% 為級距)")
    print("-" * 50)
    
    bins = [0, 1, 2, 3, 4, 5, 100]
    labels = ['0-1%', '1-2%', '2-3%', '3-4%', '4-5%', '>5%']
    
    if engine.buy_stats:
        print("【買進 - 籌碼週增幅】")
        res = pd.cut(engine.buy_stats, bins=bins, labels=labels, right=False).value_counts().sort_index()
        print(res.to_string())
    
    print("\n")
    if engine.sell_stats:
        print("【賣出 - 籌碼週減幅】")
        res = pd.cut(engine.sell_stats, bins=bins, labels=labels, right=False).value_counts().sort_index()
        print(res.to_string())
    print("="*50)

def plot_performance(df):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
    
    ax1.plot(df.index, df['Equity'], color='#d62728', linewidth=2, label='策略權益')
    
    ax1b = ax1.twinx()
    ax1b.plot(df.index, df['Benchmark'], color='blue', linestyle='--', alpha=0.4, label='大盤指數')
    
    # 標記熔斷區間
    freezing = df[df['Is_Freezing'] == 1]
    if not freezing.empty:
        ax1.fill_between(df.index, df['Equity'].min(), df['Equity'].max(), 
                         where=df['Is_Freezing']==1, color='gray', alpha=0.2, label='熔斷保護期')
    
    ax1.set_title(f"V3 Tiered Strategy vs 大盤", fontsize=14, fontweight='bold')
    ax1.set_ylabel("資產總值 (NTD)", fontsize=12)
    ax1b.set_ylabel("加權指數", fontsize=12, rotation=270, labelpad=15)
    
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax1b.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc='upper left')
    ax1.grid(True, alpha=0.3)
    
    ax2.fill_between(df.index, df['Drawdown']*100, 0, color='green', alpha=0.3)
    ax2.set_ylabel("回撤幅度 (%)", fontsize=12)
    ax2.set_title("策略回撤風險", fontsize=12)
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.show()

# ==========================================
# 5. 主程式
# ==========================================
if __name__ == "__main__":
    dh = DataHandler()
    try:
        dh.load_data()
        
        if not dh.price_data:
            print("錯誤: 無資料載入")
        else:
            engine = BacktestEngine(dh)
            res_df = engine.run()
            
            if not res_df.empty:
                res_df, trade_df = analyze_results(res_df, engine)
                print_stats(engine)
                plot_performance(res_df)
            else:
                print("無回測結果產生")
                
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()