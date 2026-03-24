import sys
import subprocess
import requests
import json

# 設定 Ollama 的本地 API 端點與使用的模型
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5-coder:7b"

def get_git_diff():
    # 取得 staged 的程式碼差異，並明確指定使用 utf-8 解碼
    result = subprocess.run(['git', 'diff', '--cached'], capture_output=True, text=True, encoding='utf-8')
    return result.stdout

def generate_commit_message(diff_text):
    if not diff_text:
        return None
    
    # 限制輸入長度，確保本地顯卡或記憶體不會 OOM (Out of Memory)
    diff_text = diff_text[:3000] 
    
    prompt = f"""
    你是一個資深的軟體工程師。請根據以下的 git diff 內容，生成一個簡潔、明確的 git commit message。
    【規範】
    1. 遵守 Conventional Commits 規範 (如: feat, fix, refactor 等)。
    2. 第一行是標題，格式為 `<type>: <subject>`，不超過 50 個字元，請使用繁體中文。
    3. 不要輸出任何解釋性的廢話，直接給我 markdown code block 以外的純文本。
    
    【Git Diff 內容】
    {diff_text}
    """
    
    # 構建請求的 Payload
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False # 設定為 False 以一次性獲取完整回覆
    }
    
    try:
        # 發送 POST 請求給本地的 Ollama
        response = requests.post(OLLAMA_API_URL, json=payload)
        response.raise_for_status()
        result = response.json()
        return result.get("response", "").strip()
    except requests.exceptions.RequestException as e:
        print(f"\n❌ 無法連線到本地 Ollama 服務，請確認 Ollama 是否已啟動。錯誤: {e}")
        return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("請提供 commit message 檔案路徑")
        sys.exit(1)
        
    commit_msg_filepath = sys.argv[1] 
    
    diff = get_git_diff()
    if not diff:
        sys.exit(0)

    print(f"🤖 本地模型 ({MODEL_NAME}) 正在推論中，請稍候...")
    ai_msg = generate_commit_message(diff)

    if ai_msg:
        with open(commit_msg_filepath, 'r+', encoding='utf-8') as f:
            original_content = f.read()
            f.seek(0, 0)
            f.write(f"{ai_msg}\n\n{original_content}")
        print("✅ Local AI Commit 生成成功！")