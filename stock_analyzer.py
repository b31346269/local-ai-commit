def calculate_sma(prices, window_size):
    """計算簡單移動平均線 (SMA)"""
    if len(prices) < window_size:
        return []
    
    sma_list = []
    for i in range(len(prices) - window_size + 1):
        window = prices[i : i + window_size]
        sma_list.append(sum(window) / window_size)
    return sma_list

if __name__ == "__main__":
    # 模擬近期科技股的價格走勢
    tech_stock_prices = [105, 108, 109, 112, 110, 115, 118]
    result = calculate_sma(tech_stock_prices, 5)
    print(f"計算出的 5日 SMA 為: {result}")